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
 * by id from behind the "Recent custom windows" disclosure, which alone reads
 * `GET /runs?kind=custom`, starts open only while that list is cached, and forgets the list when
 * it is closed, so a later picker starts closed and reads it only once opened again. No window
 * length is written into the client: every preset, the default and the longest
 * span come from the configuration, and window ends come from runs.
 *
 * Requests are answered by `../../test/mockApi`, so the real client and TanStack Query hooks
 * run; payloads come from `../../test/fixtures`; navigation is read from the test router's
 * `router.state.location`. `mockApi` is imported before `../../test/render` and Testing Library,
 * so its unmatched-request check runs after Testing Library's cleanup.
 *
 * Most expectations about the default contract (which presets the picker offers, which one is
 * the default, and which window a preset posts) are derived from the fixture configuration
 * ({@link fixtureWindow}) and the fixture default run ({@link defaultRun}). Selected cases
 * deliberately pin literal labels or hand-computed dates, so the labelling and the date
 * arithmetic are checked independently of {@link presetLabel} and `presetWindow`. None of those
 * literals is a UI default of the client.
 */
import { apiError, html, json, mockApi } from '../../test/mockApi';
import type {
  ApiMock,
  MockHandler,
  MockHandlers,
  MockResponse,
  RecordedCall,
} from '../../test/mockApi';
import { createTestQueryClient, renderWithProviders } from '../../test/render';
import { useRouterState } from '@tanstack/react-router';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../../api/queries';
import type { ConfigView, Run } from '../../api/types';
import { formatInstant } from '../../lib/format';
import {
  CUSTOM_RUN_ID,
  DEFAULT_RUN_ID,
  makeConfig,
  makeRecommendationsPage,
  makeRun,
} from '../../test/fixtures';
import { byTextContent } from '../../test/text';
import { WindowPicker, presetWindow } from './WindowPicker';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/**
 * The fixture configuration's window settings (`GET /config`): the presets, in configured
 * order, and the default that every expectation about the offered presets derives from.
 */
const fixtureWindow: ConfigView['window'] = makeConfig().window;

/**
 * The fixture's latest succeeded default run (`GET /runs/latest`): its `window_end` is the
 * default window end that every preset ends on (AAP 0.5.4.1).
 */
const defaultRun: Run = makeRun();

/** The fixture presets other than the default, in configured order. */
const otherPresets: readonly number[] = fixtureWindow.presets.filter(
  (days) => days !== fixtureWindow.default_days,
);

/** A window length as the picker words it: "1 day", otherwise "{n} days". */
function daysLabel(days: number): string {
  return days === 1 ? '1 day' : `${String(days)} days`;
}

/**
 * The accessible name `DateRangePicker` gives the preset of `days`: {@link daysLabel}, with
 * " (default)" on the preset equal to `defaultDays`.
 */
function presetLabel(days: number, defaultDays: number = fixtureWindow.default_days): string {
  return days === defaultDays ? `${daysLabel(days)} (default)` : daysLabel(days);
}

/** Every preset's accessible name under `window`, in configured order. */
function presetLabels(window: ConfigView['window'] = fixtureWindow): string[] {
  return window.presets.map((days) => presetLabel(days, window.default_days));
}

/**
 * The reopen-select label of a custom run: "{start} – {end}" when `copyNumber` is 1, that is
 * for the only or the oldest listed run of its window, and "{start} – {end} ({copyNumber})"
 * for each later listed run of that window, counted from the oldest.
 */
function reopenLabel(run: Run, copyNumber: number): string {
  const range = `${run.window_start} – ${run.window_end}`;
  return copyNumber === 1 ? range : `${range} (${String(copyNumber)})`;
}

/**
 * The hint under the reopen select for the custom run it shows: "Run {id} · {days} days ·
 * computed {finished_at in the run's display time zone}".
 */
function reopenHint(run: Run): string {
  return `Run ${String(run.id)} · ${daysLabel(run.days)} · computed ${formatInstant(run.finished_at, run.display_timezone)}`;
}

/**
 * The accessible description of `element`: the text of each element its `aria-describedby`
 * names, in order, joined by a space; `null` when it names none.
 */
function accessibleDescription(element: HTMLElement): string | null {
  const ids = (element.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean);
  if (ids.length === 0) {
    return null;
  }
  return ids.map((id) => document.getElementById(id)?.textContent ?? '').join(' ');
}

/**
 * The polite `role="status"` region whose whole text is `label`, such as a `Spinner`'s, or
 * `undefined`. A status region takes no accessible name from its content, so it is found by its
 * text rather than by `{ name }`.
 */
function statusWithText(label: string): HTMLElement | undefined {
  return screen
    .queryAllByRole('status')
    .find((region) => (region.textContent ?? '').trim() === label);
}

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
 * default run, no custom run and a `POST /runs` that creates {@link customRun}, plus the
 * created run's page, `GET /recommendations?run_id=<id>`, which the mutation reads before the
 * picker opens the run. `overrides` replaces any of them.
 */
function stubApi(overrides: MockHandlers = {}): ApiMock {
  return mockApi({
    'GET /config': json(200, makeConfig()),
    'GET /runs/latest': json(200, makeRun()),
    'GET /runs': json(200, []),
    'POST /runs': json(201, customRun),
    'GET /recommendations': (call) =>
      json(
        200,
        makeRecommendationsPage({ run: { ...customRun, id: Number(call.search.get('run_id')) } }),
      ),
    ...overrides,
  });
}

/** The `run_id` of every `GET /recommendations` request made so far, in order. */
function pageReads(api: ApiMock): (string | null)[] {
  return api.calls
    .filter((call) => call.method === 'GET' && call.path === '/recommendations')
    .map((call) => call.search.get('run_id'));
}

/** Renders the picker for `run` at `path`. */
function renderPicker(run: Run = makeRun(), path = '/actions') {
  return renderWithProviders(<WindowPicker run={run} />, { path });
}

/** Props of {@link RunFromUrl}. */
interface RunFromUrlProps {
  /** The runs `?run=<id>` can show. */
  runs: readonly Run[];
}

/**
 * One picker that stays mounted while the run on screen follows the URL, as on `/actions`: the
 * entry of `runs` whose id is `?run=`, or else the fixture default run.
 */
function RunFromUrl({ runs }: RunFromUrlProps): ReactElement {
  const runId = useRouterState({ select: (state) => state.location.search.run });
  return <WindowPicker run={runs.find((candidate) => candidate.id === runId) ?? defaultRun} />;
}

/** Renders {@link RunFromUrl} for `runs` at `path`. */
function renderRunFromUrl(runs: readonly Run[], path = '/actions') {
  return renderWithProviders(<RunFromUrl runs={runs} />, { path });
}

/** A preset or picker button by its accessible name. */
function button(name: string): HTMLElement {
  return screen.getByRole('button', { name });
}

/**
 * Presses "Recent custom windows", which opens the recent custom runs and so reads
 * `GET /runs?kind=custom`.
 *
 * @returns The disclosure button.
 */
function openRecentCustomRuns(): HTMLElement {
  const toggle = button('Recent custom windows');
  fireEvent.click(toggle);
  return toggle;
}

/** The `GET /runs?kind=custom` requests made so far. */
function customRunReads(api: ApiMock): RecordedCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/runs');
}

/** The region the "Recent custom windows" button names in `aria-controls`. */
function recentCustomRunsRegion(): HTMLElement {
  const id = button('Recent custom windows').getAttribute('aria-controls') ?? '';
  const region = id === '' ? null : document.getElementById(id);
  if (region === null) {
    throw new Error('The "Recent custom windows" button controls no element.');
  }
  return region;
}

/**
 * The picker's own polite status region, which holds the "Computing custom window" spinner and
 * every warning: the last child of the picker's frame, below the recent custom windows region.
 */
function frameStatusRegion(): HTMLElement {
  const region = recentCustomRunsRegion().parentElement?.lastElementChild;
  if (!(region instanceof HTMLElement) || region.getAttribute('role') !== 'status') {
    throw new Error('The picker’s frame ends in no status region.');
  }
  return region;
}

/**
 * The visually hidden status region at the end of the date-range picker, which announces the
 * window the picker shows (`DateRangePicker`'s `statusMessage`).
 */
function openedStatusRegion(): HTMLElement {
  const picker = screen.getByRole('group', { name: 'Analysis window' }).closest('.ds-daterange');
  const region = picker?.lastElementChild;
  if (!(region instanceof HTMLElement) || region.getAttribute('role') !== 'status') {
    throw new Error('The date-range picker ends in no status region.');
  }
  return region;
}

/**
 * The text the picker announces once a custom run is on screen. It lands in
 * {@link openedStatusRegion} shortly after the region is committed, never in the same task, so an
 * expectation of it waits for it.
 */
function openedMessage(run: Run): string {
  return `Custom window ${run.window_start} – ${run.window_end} opened, ${daysLabel(run.days)}, run ${String(run.id)}`;
}

/**
 * How long `DateRangePicker` holds a status message back after the commit (its
 * `STATUS_MESSAGE_DELAY_MS`), so its region is in the accessibility tree before the text changes.
 */
const OPENED_MESSAGE_DELAY_MS = 100;

/**
 * Waits until a status message the picker still holds back would have landed, so an expectation
 * that {@link openedStatusRegion} stays empty cannot pass merely because a message is pending.
 */
async function openedMessageSettled(): Promise<void> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        window.setTimeout(resolve, OPENED_MESSAGE_DELAY_MS);
      }),
  );
}

/** The "Reopen" button below the reopen select, which opens the custom run it shows. */
function reopenButton(): HTMLElement {
  return button('Reopen');
}

/** Waits until the picker's controls are on screen. */
async function presetsShown(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByRole('group', { name: 'Analysis window' })).toBeTruthy();
  });
}

/** The text labels (`textContent`) of the preset group's buttons, in order. */
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
 * Leaves focus on the page body, as a browser does once a focused control is natively disabled
 * (the focus fixup rule; a date field, say, since a `Button` keeps focus), or after a click on
 * the page background. jsdom implements no fixup, and its `blur()` ignores a disabled element, so
 * focus is moved to a temporary focusable element that is then removed, which jsdom does
 * follow by returning focus to the body.
 */
function dropFocusToBody(): void {
  const holder = document.createElement('span');
  holder.tabIndex = -1;
  document.body.append(holder);
  act(() => {
    holder.focus();
    holder.remove();
  });
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
  it('gives the 28 days ending 2026-10-01 as 2026-09-04 to 2026-10-01 (AAP 0.5.4.1)', () => {
    expect(presetWindow(28, '2026-10-01')).toEqual({
      window_start: '2026-09-04',
      window_end: '2026-10-01',
    });
  });

  it('crosses a year boundary by calendar dates', () => {
    expect(presetWindow(14, '2026-01-05')).toEqual({
      window_start: '2025-12-23',
      window_end: '2026-01-05',
    });
  });

  it('gives the same calendar dates whatever time zone the process runs in', () => {
    const cases = [
      [28, '2026-10-01'],
      [14, '2026-01-05'],
      [1, '2026-10-01'],
      // A daylight-saving change inside the window: 2026-03-08 in America/Los_Angeles, and
      // 2026-03-29 in Europe/London.
      [60, '2026-04-15'],
    ] as const;
    const expected = cases.map(([days, end]) => presetWindow(days, end));
    // Zones on both sides of UTC, a half-hour offset and one 14 hours ahead, so a helper that
    // read local time would move a date by a day in at least one of them. Each carries its
    // `getTimezoneOffset()` at 2026-07-01 12:00 UTC, which proves the zone took effect.
    const zones = [
      { zone: 'Pacific/Kiritimati', offset: -840 },
      { zone: 'America/Los_Angeles', offset: 420 },
      { zone: 'Asia/Kolkata', offset: -330 },
      { zone: 'Europe/London', offset: -60 },
    ] as const;
    const probe = new Date(Date.UTC(2026, 6, 1, 12));
    try {
      for (const { zone, offset } of zones) {
        vi.stubEnv('TZ', zone);
        expect(probe.getTimezoneOffset()).toBe(offset);
        expect(cases.map(([days, end]) => presetWindow(days, end))).toEqual(expected);
      }
    } finally {
      vi.unstubAllEnvs();
    }
  });

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

      // Before GET /config answers: a polite "Loading windows" status, and no preset at all.
      expect(statusWithText('Loading windows')).toBeDefined();
      expect(screen.queryByRole('group', { name: 'Analysis window' })).toBeNull();
      expect(screen.queryByRole('button')).toBeNull();

      const shipped = makeConfig();
      config.resolve(json(200, shipped));
      await presetsShown();

      expect(statusWithText('Loading windows')).toBeUndefined();
      // The preset buttons are exactly the configured presets, in configured order.
      const presets = presetNames().filter((name) => /^\d+ days?/u.test(name));
      expect(presets.map((name) => Number(/^(\d+)/u.exec(name)?.[1]))).toEqual(
        shipped.window.presets,
      );
      expect(presets).toEqual(presetLabels(shipped.window));
      // Exactly one is marked "(default)": the configured default.
      expect(presets.filter((name) => name.endsWith('(default)'))).toEqual([
        `${daysLabel(shipped.window.default_days)} (default)`,
      ]);
      // The custom range follows the presets.
      expect(presetNames().at(-1)).toBe('Custom range');
      // The run on screen is the latest default run, so the default preset alone is pressed.
      for (const days of shipped.window.presets) {
        const preset = button(presetLabel(days, shipped.window.default_days));
        expect(preset.getAttribute('aria-pressed')).toBe(
          String(days === shipped.window.default_days),
        );
      }
    });

    it('shows a spinner until GET /runs/latest first answers', async () => {
      const latest = deferred();
      const api = stubApi({ 'GET /runs/latest': latest.handler });
      const { queryClient } = await renderPicker();

      // GET /config has answered and GET /runs?kind=custom waits until the recent custom
      // windows are opened; only the held GET /runs/latest is still fetching, so only the
      // default run holds the picker back.
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(1);
      });
      expect(api.calls.map((call) => `${call.method} ${call.path}`).sort()).toEqual([
        'GET /config',
        'GET /runs/latest',
      ]);
      expect(statusWithText('Loading windows')).toBeDefined();
      expect(screen.queryByRole('group', { name: 'Analysis window' })).toBeNull();

      latest.resolve(json(200, makeRun()));
      await presetsShown();
      expect(statusWithText('Loading windows')).toBeUndefined();
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

    it('moves "(default)" to a newly configured default preset', async () => {
      // The shipped presets plus a 21-day preset, which the configuration makes the default.
      const changedDefault = 21;
      const previousDefault = fixtureWindow.default_days;
      const presets = [...fixtureWindow.presets, changedDefault].sort((a, b) => a - b);
      const changed = makeConfig({
        window: { ...fixtureWindow, presets, default_days: changedDefault },
      });
      const run = makeRun({
        ...presetWindow(changedDefault, defaultRun.window_end),
        days: changedDefault,
        default_days: changedDefault,
      });
      stubApi({ 'GET /config': json(200, changed), 'GET /runs/latest': json(200, run) });
      await renderPicker(run);
      await presetsShown();

      expect(presetNames()).toEqual([...presetLabels(changed.window), 'Custom range']);
      const marked = presetNames().filter((name) => name.endsWith('(default)'));
      expect(marked).toEqual([`${daysLabel(changedDefault)} (default)`]);
      expect(button(`${daysLabel(changedDefault)} (default)`).getAttribute('aria-pressed')).toBe(
        'true',
      );
      // The previous default is still offered, as a plain preset.
      expect(button(daysLabel(previousDefault)).getAttribute('aria-pressed')).toBe('false');
      expect(
        screen.queryByRole('button', { name: `${daysLabel(previousDefault)} (default)` }),
      ).toBeNull();
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
      expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      // The custom run is one of the presets and ends on the default window end, so its preset
      // is pressed rather than the default.
      expect(button(presetLabel(customRun.days)).getAttribute('aria-pressed')).toBe('true');
      const defaultPreset = button(presetLabel(fixtureWindow.default_days));
      expect(defaultPreset.getAttribute('aria-pressed')).toBe('false');

      fireEvent.click(defaultPreset);

      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      expect(router.state.location.pathname).toBe('/actions');
      expect(router.state.location.search.run).toBeUndefined();
      expect(router.state.location.searchStr).toBe('');
      // The default window is the worker's default run: choosing it never posts POST /runs.
      expect(posts(api)).toHaveLength(0);
      expect(api.calls.some((call) => call.method === 'POST')).toBe(false);
    });

    it('computes any other preset ending on the default window end, then opens the run by id', async () => {
      // A preset of the fixture configuration that is not its default.
      const days = otherPresets[0];
      if (days === undefined) {
        throw new Error('The fixture configuration offers no preset besides its default.');
      }
      const expectedBody = presetWindow(days, defaultRun.window_end);
      const created = makeRun({
        id: CUSTOM_RUN_ID,
        kind: 'custom',
        trigger: 'manual',
        ...expectedBody,
        days,
      });
      const api = stubApi({ 'POST /runs': json(201, created) });
      const { router } = await renderPicker(defaultRun);
      await presetsShown();

      fireEvent.click(button(presetLabel(days)));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      });
      expect(router.state.location.search.run).toBe(CUSTOM_RUN_ID);
      expect(router.state.location.pathname).toBe('/actions');

      const sent = posts(api);
      expect(sent).toHaveLength(1);
      const [post] = sent;
      // The preset's window, ending on the latest default run's window end.
      expect(post?.body).toEqual(expectedBody);
      expect((post?.body as typeof expectedBody | undefined)?.window_end).toBe(
        defaultRun.window_end,
      );
      // The client marks every mutating request for the API's CSRF check (AAP 0.3.2).
      expect(post?.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post?.headers.get('Content-Type')).toMatch(/^application\/json/u);
      // The created run's page was read before the picker opened it, so it renders at once.
      expect(pageReads(api)).toEqual([String(CUSTOM_RUN_ID)]);
    });

    it('disables every control and states "Computing custom window" while the run computes', async () => {
      const days = otherPresets.at(-1);
      if (days === undefined) {
        throw new Error('The fixture configuration offers no preset besides its default.');
      }
      const created = deferred();
      const api = stubApi({
        'GET /runs': json(200, [customRun]),
        'POST /runs': created.handler,
      });
      const { router } = await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      await waitFor(() => {
        expect(screen.getByRole('combobox', { name: 'Reopen a custom window' })).toBeTruthy();
      });
      expect(statusWithText('Computing custom window')).toBeUndefined();
      // The picker's own status region is in place, empty, before the run starts.
      const statusRegion = frameStatusRegion();
      expect(statusRegion.childNodes).toHaveLength(0);

      fireEvent.click(button(presetLabel(days)));

      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      expect(posts(api)[0]?.body).toEqual(presetWindow(days, defaultRun.window_end));

      // While the run computes, a polite status says so and every control is disabled. The
      // spinner lands inside the status region that existed before, so it is announced.
      await waitFor(() => {
        expect(statusWithText('Computing custom window')).toBeDefined();
      });
      expect(frameStatusRegion()).toBe(statusRegion);
      expect((statusRegion.textContent ?? '').trim()).toBe('Computing custom window');
      // The spinner's own status sits inside the region, not beside it.
      expect(
        within(statusRegion)
          .getAllByRole('status')
          .map((inner) => (inner.textContent ?? '').trim()),
      ).toEqual(['Computing custom window']);
      expect(screen.queryByRole('alert')).toBeNull();
      for (const name of [...presetLabels(), 'Custom range']) {
        expect(button(name).hasAttribute('disabled')).toBe(true);
      }
      expect(
        screen.getByRole('combobox', { name: 'Reopen a custom window' }).hasAttribute('disabled'),
      ).toBe(true);
      expect(reopenButton().hasAttribute('disabled')).toBe(true);
      expect(toggle.hasAttribute('disabled')).toBe(true);
      expect(router.state.location.search).toEqual({});

      created.resolve(json(201, customRun));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      });
      expect(router.state.location.pathname).toBe('/actions');
      await waitFor(() => {
        expect(statusWithText('Computing custom window')).toBeUndefined();
      });
      // The same region stays, empty again.
      expect(frameStatusRegion()).toBe(statusRegion);
      expect(statusRegion.childNodes).toHaveLength(0);
      for (const name of [...presetLabels(), 'Custom range']) {
        expect(button(name).hasAttribute('disabled')).toBe(false);
      }
      expect(toggle.hasAttribute('disabled')).toBe(false);
    });

    it('shows the computing spinner before any warning, in the same status region', async () => {
      const created = deferred();
      stubApi({
        'GET /runs/latest': apiError(500, 'internal', 'The server failed.'),
        'POST /runs': created.handler,
      });
      await renderPicker();
      await presetsShown();
      await waitFor(() => {
        expect(noticeTitles()).toEqual(['Could not read the latest default run']);
      });
      const statusRegion = frameStatusRegion();
      expect(noticeRegion()).toBe(statusRegion);

      fireEvent.click(button('56 days'));

      await waitFor(() => {
        expect(statusWithText('Computing custom window')).toBeDefined();
      });
      expect(frameStatusRegion()).toBe(statusRegion);
      const spinner = within(statusRegion)
        .getAllByRole('status')
        .find((inner) => (inner.textContent ?? '').trim() === 'Computing custom window');
      const banner = statusRegion.querySelector('.ds-banner');
      // The spinner comes first, then the warning it does not replace.
      expect(
        banner !== null &&
          spinner !== undefined &&
          (spinner.compareDocumentPosition(banner) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
      ).toBe(true);
      expect(noticeTitles()).toEqual(['Could not read the latest default run']);

      created.resolve(apiError(409, 'run_in_progress', 'An analysis is already running.'));
      await waitFor(() => {
        expect(statusWithText('Computing custom window')).toBeUndefined();
      });
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
      for (const name of presetLabels()) {
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
      fireEvent.click(button(presetLabel(fixtureWindow.default_days)));
      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      expect(
        screen.queryByText(
          byTextContent(
            '.ds-banner__title',
            'A window of 14 days ending 0001-01-01 would start before the year 1.',
          ),
        ),
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
      // The created run's page was read before the picker opened it, so it renders at once.
      expect(pageReads(api)).toEqual([String(CUSTOM_RUN_ID)]);
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
    it('reads "Another analysis is running" for 409 run_in_progress, stays put and keeps focus', async () => {
      const created = deferred();
      const api = stubApi({ 'POST /runs': created.handler });
      const { router } = await renderPicker();
      await presetsShown();

      const preset = button('56 days');
      act(() => {
        preset.focus();
      });
      fireEvent.click(preset);
      // The focused preset is disabled while the run computes, but keeps focus: it is inert and
      // aria-disabled until focus leaves it, while the other presets are natively disabled.
      await waitFor(() => {
        expect(preset.getAttribute('aria-disabled')).toBe('true');
      });
      expect(preset.hasAttribute('disabled')).toBe(false);
      expect(document.activeElement).toBe(preset);
      expect(button('14 days').hasAttribute('disabled')).toBe(true);

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
      // The picker is usable again, and focus is still on the control that started the run.
      await waitFor(() => {
        expect(preset.hasAttribute('aria-disabled')).toBe(false);
      });
      expect(preset.hasAttribute('disabled')).toBe(false);
      expect(document.activeElement).toBe(preset);
    });

    it('shows the API’s own message for a 422, until the next choice clears it', async () => {
      const message = 'Window end is after the default window end';
      stubApi({ 'POST /runs': apiError(422, 'invalid_window', message) });
      const { router } = await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();

      fireEvent.click(button('14 days'));

      await waitFor(() => {
        expect(noticeTitles()).toEqual([message]);
      });
      expect(screen.getByText(message)).toBeTruthy();
      expect(screen.queryByText('Another analysis is running. Try again shortly.')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
      expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });

      fireEvent.click(button(presetLabel(fixtureWindow.default_days)));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      expect(screen.queryByText(message)).toBeNull();
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

  describe('one computation at a time', () => {
    /** The warning a 409 `run_in_progress` shows. */
    const RUN_IN_PROGRESS = 'Another analysis is running. Try again shortly.';

    /**
     * A preset of the fixture configuration other than its default and other than `except`, in
     * configured order.
     */
    function otherPreset(except?: number): number {
      const days = otherPresets.find((candidate) => candidate !== except);
      if (days === undefined) {
        throw new Error('The fixture configuration offers no preset besides its default.');
      }
      return days;
    }

    /**
     * The custom run `POST /runs` creates for the fixture preset of `days`: that preset's window,
     * ending on the default window end, under `id`.
     */
    function presetRun(days: number, id: Run['id'] = CUSTOM_RUN_ID): Run {
      return makeRun({
        id,
        kind: 'custom',
        trigger: 'manual',
        ...presetWindow(days, defaultRun.window_end),
        days,
      });
    }

    /**
     * A `POST /runs` handler that answers as the API's analysis lock does while one computation
     * runs: the first request waits for `first`, and every request that arrives meanwhile is
     * refused with 409 `run_in_progress`.
     */
    function lockedWhileComputing(first: Deferred): MockHandler {
      let computing = false;
      return () => {
        if (computing) {
          return apiError(409, 'run_in_progress', 'An analysis is already running.');
        }
        computing = true;
        return first.handler().finally(() => {
          computing = false;
        });
      };
    }

    /**
     * Two presses of one control that both land before the picker can stop the second by its
     * rendered state: two clicks in a row, as a double click delivers them, and two clicks in
     * one task, as a script delivers them, with no render between them.
     */
    const doublePresses = [
      {
        title: 'two clicks in a row',
        press: (target: HTMLElement) => {
          fireEvent.click(target);
          fireEvent.click(target);
        },
      },
      {
        title: 'two clicks in one task',
        press: (target: HTMLElement) => {
          act(() => {
            target.click();
            target.click();
          });
        },
      },
    ] as const;

    it.each(doublePresses)(
      'posts a preset once for $title, and opens the run it created',
      async ({ press }) => {
        const days = otherPreset();
        const created = presetRun(days);
        const answer = deferred();
        const api = stubApi({ 'POST /runs': lockedWhileComputing(answer) });
        const { router } = await renderPicker();
        await presetsShown();

        press(button(presetLabel(days)));

        await waitFor(() => {
          expect(posts(api)).toHaveLength(1);
        });
        answer.resolve(json(201, created));

        await waitFor(() => {
          expect(router.state.location.search).toEqual({ run: created.id });
        });
        expect(router.state.location.pathname).toBe('/actions');
        expect(posts(api).map((call) => call.body)).toEqual([
          presetWindow(days, defaultRun.window_end),
        ]);
        expect(screen.queryByText(RUN_IN_PROGRESS)).toBeNull();
        await waitFor(() => {
          expect(statusWithText('Computing custom window')).toBeUndefined();
        });
      },
    );

    it('disables every control and shows the computing spinner from the press that starts a run', async () => {
      const days = otherPreset();
      const created = presetRun(days);
      const answer = deferred();
      const api = stubApi({
        'GET /runs': json(200, [customRun]),
        'POST /runs': answer.handler,
      });
      const { router } = await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      // The custom panel is opened first, so its fields and Apply are on screen too.
      fireEvent.click(button('Custom range'));
      const start = screen.getByLabelText<HTMLInputElement>(/^Start date/u);
      const end = screen.getByLabelText<HTMLInputElement>(/^End date/u);
      expect(start.disabled).toBe(false);

      fireEvent.click(button(presetLabel(days)));

      // Checked at once, with no wait: the press itself has disabled the picker, before the
      // POST has answered.
      expect(statusWithText('Computing custom window')).toBeDefined();
      for (const name of [...presetLabels(), 'Custom range', 'Apply']) {
        expect(button(name).hasAttribute('disabled')).toBe(true);
      }
      expect(start.disabled).toBe(true);
      expect(end.disabled).toBe(true);
      expect(toggle.hasAttribute('disabled')).toBe(true);
      expect(select.hasAttribute('disabled')).toBe(true);
      expect(reopenButton().hasAttribute('disabled')).toBe(true);

      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      answer.resolve(json(201, created));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: created.id });
      });
      await waitFor(() => {
        expect(button('Custom range').hasAttribute('disabled')).toBe(false);
      });
      expect(toggle.hasAttribute('disabled')).toBe(false);
      expect(posts(api)).toHaveLength(1);
    });

    it('ignores the default preset pressed in the same task as a computing press, and still opens the computed run', async () => {
      // The run on screen is a custom run, so a choice of the default preset would navigate.
      const days = otherPreset(customRun.days);
      const created = presetRun(days, CUSTOM_RUN_ID + 8);
      const answer = deferred();
      const api = stubApi({ 'POST /runs': lockedWhileComputing(answer) });
      const { router } = await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();
      const entriesBefore = router.history.length;

      act(() => {
        button(presetLabel(days)).click();
        button(presetLabel(fixtureWindow.default_days)).click();
      });

      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      // The default preset neither navigated nor reset the computation.
      expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      expect(router.history.length).toBe(entriesBefore);
      expect(statusWithText('Computing custom window')).toBeDefined();

      answer.resolve(json(201, created));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: created.id });
      });
      // One navigation only: to the computed run.
      expect(router.history.length).toBe(entriesBefore + 1);
      expect(posts(api).map((call) => call.body)).toEqual([
        presetWindow(days, defaultRun.window_end),
      ]);
      expect(screen.queryByText(RUN_IN_PROGRESS)).toBeNull();
    });

    it('ignores Reopen and the recent custom windows toggle pressed in the same task as a computing press', async () => {
      const days = otherPreset(customRun.days);
      const created = presetRun(days, CUSTOM_RUN_ID + 8);
      const answer = deferred();
      const api = stubApi({
        'GET /runs': json(200, [customRun]),
        'POST /runs': answer.handler,
      });
      const { router } = await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      fireEvent.change(select, { target: { value: String(customRun.id) } });
      const reads = customRunReads(api).length;

      act(() => {
        button(presetLabel(days)).click();
        reopenButton().click();
        toggle.click();
      });

      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      // Reopen opened nothing, and the list stayed open and was neither forgotten nor read.
      expect(router.state.location.search).toEqual({});
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(customRunReads(api)).toHaveLength(reads);

      answer.resolve(json(201, created));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: created.id });
      });
    });

    it('ignores both Retry buttons pressed in the same task as a computing press', async () => {
      const days = otherPreset();
      const created = presetRun(days);
      const answer = deferred();
      const api = stubApi({
        'GET /runs/latest': apiError(500, 'internal_error', 'The database is unavailable.'),
        'GET /runs': apiError(503, 'unavailable', 'The API is restarting.'),
        'POST /runs': answer.handler,
      });
      const { router } = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();
      await waitFor(() => {
        expect(noticeTitles()).toEqual([
          'Could not read the latest default run',
          'Could not load recent custom windows',
        ]);
      });
      const latestReads = (): number =>
        api.calls.filter((call) => call.method === 'GET' && call.path === '/runs/latest').length;
      const latestBefore = latestReads();
      const customBefore = customRunReads(api).length;

      act(() => {
        button(presetLabel(days)).click();
        button('Retry reading the latest default run').click();
        button('Retry loading recent custom windows').click();
      });

      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      expect(latestReads()).toBe(latestBefore);
      expect(customRunReads(api)).toHaveLength(customBefore);

      answer.resolve(json(201, created));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: created.id });
      });
    });

    it.each(doublePresses)(
      'posts a custom range once for $title on Apply, and opens the run it created',
      async ({ press }) => {
        const answer = deferred();
        const api = stubApi({ 'POST /runs': lockedWhileComputing(answer) });
        const { router } = await renderPicker();
        await presetsShown();
        fireEvent.click(button('Custom range'));
        fireEvent.change(screen.getByLabelText(/^Start date/u), {
          target: { value: '2026-08-01' },
        });
        fireEvent.change(screen.getByLabelText(/^End date/u), {
          target: { value: '2026-08-30' },
        });

        press(button('Apply'));

        await waitFor(() => {
          expect(posts(api)).toHaveLength(1);
        });
        answer.resolve(json(201, customRun));

        await waitFor(() => {
          expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
        });
        expect(posts(api).map((call) => call.body)).toEqual([
          { window_start: '2026-08-01', window_end: '2026-08-30' },
        ]);
        expect(screen.queryByText(RUN_IN_PROGRESS)).toBeNull();
      },
    );

    it('frees the picker after a 409 from a run another process holds, so a later choice posts again', async () => {
      const days = otherPreset();
      const created = presetRun(days);
      const api = stubApi({
        'POST /runs': apiError(409, 'run_in_progress', 'An analysis is already running.'),
      });
      const { router } = await renderPicker();
      await presetsShown();

      fireEvent.click(button(presetLabel(days)));

      await waitFor(() => {
        expect(noticeTitles()).toEqual([RUN_IN_PROGRESS]);
      });
      expect(noticeRegion().getAttribute('role')).toBe('status');
      expect(screen.queryByRole('alert')).toBeNull();
      expect(router.state.location.search).toEqual({});
      await waitFor(() => {
        expect(button(presetLabel(days)).hasAttribute('disabled')).toBe(false);
      });

      api.use({ 'POST /runs': json(201, created) });
      fireEvent.click(button(presetLabel(days)));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: created.id });
      });
      expect(posts(api)).toHaveLength(2);
      expect(screen.queryByText(RUN_IN_PROGRESS)).toBeNull();
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
      expect(button(presetLabel(fixtureWindow.default_days)).getAttribute('aria-pressed')).toBe(
        'true',
      );

      // A retry that fails again keeps the warning, with the new reason, and focus on Retry.
      const latest = deferred();
      api.use({ 'GET /runs/latest': latest.handler });
      const retry = button('Retry reading the latest default run');
      expect(retry.textContent).toBe('Retry');
      act(() => {
        retry.focus();
      });
      fireEvent.click(retry);

      await waitFor(() => {
        expect(retry.getAttribute('aria-busy')).toBe('true');
      });
      // Inert while loading but never natively disabled, so it keeps focus.
      expect(retry.getAttribute('aria-disabled')).toBe('true');
      expect(retry.hasAttribute('disabled')).toBe(false);
      expect(document.activeElement).toBe(retry);
      // The warning is kept in place while the read runs again.
      expect(noticeTitles()).toEqual(['Could not read the latest default run']);
      expect(retry.isConnected).toBe(true);
      // Focus lost to the page body meanwhile still goes back to Retry once the read settles.
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
      openRecentCustomRuns();

      await waitFor(() => {
        expect(noticeTitles()).toEqual(['Could not load recent custom windows']);
      });
      expect(within(noticeRegion()).getByText('Could not list runs.')).toBeTruthy();
      expect(screen.queryByRole('combobox', { name: 'Reopen a custom window' })).toBeNull();
      // Neither a spinner nor "No custom windows yet": the warning states the failed read.
      expect(statusWithText('Loading recent custom windows')).toBeUndefined();
      expect(screen.queryByText('No custom windows yet')).toBeNull();
      expect(recentCustomRunsRegion().hidden).toBe(true);
      const customCall = customRunReads(api)[0];
      expect(customCall?.search.get('kind')).toBe('custom');

      api.use({ 'GET /runs': json(200, [customRun]) });
      fireEvent.click(button('Retry loading recent custom windows'));

      await waitFor(() => {
        expect(screen.getByRole('combobox', { name: 'Reopen a custom window' })).toBeTruthy();
      });
      expect(screen.queryByText('Could not load recent custom windows')).toBeNull();
    });

    it('states a failed GET /runs?kind=custom only while the recent custom windows are open', async () => {
      const api = stubApi({
        'GET /runs': apiError(500, 'internal_error', 'Could not list runs.'),
      });
      await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      await waitFor(() => {
        expect(noticeTitles()).toEqual(['Could not load recent custom windows']);
      });

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(screen.queryByText('Could not load recent custom windows')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Retry loading recent custom windows' })).toBeNull();
      expect(customRunReads(api)).toHaveLength(1);
    });

    it('states the computation first, then the default-run read, then the custom-runs read', async () => {
      stubApi({
        'GET /runs/latest': apiError(500, 'internal_error', 'The database is unavailable.'),
        'GET /runs': apiError(503, 'unavailable', 'The API is restarting.'),
        'POST /runs': apiError(409, 'run_in_progress', 'An analysis is already running.'),
      });
      await renderPicker();
      await presetsShown();
      openRecentCustomRuns();
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

    it('lists the custom runs as "{start} – {end}" and opens the chosen one by id on Reopen', async () => {
      const runs = [september, customRun] as const;
      const api = stubApi({ 'GET /runs': json(200, runs) });
      const { router } = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      // The two runs have different windows, so each is labelled by its range alone.
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        ...runs.map((run) => reopenLabel(run, 1)),
      ]);
      expect(Array.from(select.options, (option) => option.value)).toEqual([
        '',
        ...runs.map((run) => String(run.id)),
      ]);
      // The default run is on screen: it is never listed, and no custom run is chosen.
      expect(Array.from(select.options, (option) => option.value)).not.toContain(
        String(DEFAULT_RUN_ID),
      );
      expect(select.value).toBe('');
      // The empty choice names no run, so the select has no hint and no description.
      expect(accessibleDescription(select)).toBeNull();

      const [, second] = runs;
      fireEvent.change(select, { target: { value: String(second.id) } });

      // Choosing only records the choice, which the hint then describes; "Reopen" opens it.
      expect(select.value).toBe(String(second.id));
      expect(accessibleDescription(select)).toBe(reopenHint(second));
      expect(router.state.location.search).toEqual({});
      fireEvent.click(reopenButton());

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: second.id });
      });
      expect(router.state.location.pathname).toBe('/actions');
      // Reopening navigates by id; it never computes a run.
      expect(posts(api)).toHaveLength(0);
    });

    it('never navigates while the user moves through the options (WCAG 2.2 SC 3.2.2)', async () => {
      const runs = [september, customRun] as const;
      const api = stubApi({ 'GET /runs': json(200, runs) });
      const { router, queryClient } = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });

      // Each change is what ArrowDown, then ArrowDown again, then ArrowUp fire on a native select.
      for (const value of [String(september.id), String(customRun.id), String(september.id)]) {
        fireEvent.change(select, { target: { value } });
        expect(select.value).toBe(value);
        expect(reopenButton().hasAttribute('disabled')).toBe(false);
      }
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      expect(router.state.location.pathname).toBe('/actions');
      expect(router.state.location.search).toEqual({});
      expect(posts(api)).toHaveLength(0);
      // Nothing was opened, so the select is the same element, still showing the last choice.
      expect(screen.getByRole('combobox', { name: 'Reopen a custom window' })).toBe(select);
      expect(select.value).toBe(String(september.id));
    });

    it('disables Reopen on the empty choice, and enables it once a custom run is chosen', async () => {
      stubApi({ 'GET /runs': json(200, [september, customRun]) });
      const { router } = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });

      // The default run is on screen: the select starts on the empty choice.
      expect(select.value).toBe('');
      expect(reopenButton().hasAttribute('disabled')).toBe(true);

      fireEvent.change(select, { target: { value: String(september.id) } });
      expect(reopenButton().hasAttribute('disabled')).toBe(false);

      fireEvent.change(select, { target: { value: '' } });
      expect(select.value).toBe('');
      expect(reopenButton().hasAttribute('disabled')).toBe(true);
      // A disabled button fires no click.
      fireEvent.click(reopenButton());
      expect(router.state.location.search).toEqual({});
    });

    it('disables the select and Reopen while a run computes', async () => {
      const created = deferred();
      const api = stubApi({
        'GET /runs': json(200, [september, customRun]),
        'POST /runs': created.handler,
      });
      const { router } = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      fireEvent.change(select, { target: { value: String(september.id) } });
      expect(reopenButton().hasAttribute('disabled')).toBe(false);

      fireEvent.click(button('56 days'));
      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      await waitFor(() => {
        expect(reopenButton().hasAttribute('disabled')).toBe(true);
      });
      expect(select.hasAttribute('disabled')).toBe(true);
      fireEvent.click(reopenButton());
      expect(router.state.location.search).toEqual({});

      created.resolve(apiError(409, 'run_in_progress', 'An analysis is already running.'));
      await waitFor(() => {
        expect(reopenButton().hasAttribute('disabled')).toBe(false);
      });
      expect(select.hasAttribute('disabled')).toBe(false);
      // The choice made before the computation is kept.
      expect(select.value).toBe(String(september.id));
    });

    it('drops a choice not yet opened once another run is on screen', async () => {
      stubApi({ 'GET /runs': json(200, [september, customRun]) });
      const { router } = await renderRunFromUrl([september, customRun]);
      await presetsShown();
      openRecentCustomRuns();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      fireEvent.change(select, { target: { value: String(september.id) } });

      // Another run comes on screen without Reopen, for example through a preset or Back.
      await act(() => router.navigate({ to: '/actions', search: { run: customRun.id } }));

      await waitFor(() => {
        expect(select.value).toBe(String(customRun.id));
      });
      expect(reopenButton().hasAttribute('disabled')).toBe(false);

      // Back on the default run, the earlier choice does not return: the empty choice shows.
      await act(() => router.navigate({ to: '/actions', search: {} }));
      await waitFor(() => {
        expect(select.value).toBe('');
      });
      expect(reopenButton().hasAttribute('disabled')).toBe(true);
    });

    it('lists only succeeded custom runs, newest first as the API orders them', async () => {
      stubApi({ 'GET /runs': json(200, [september, failed, customRun]) });
      await renderPicker();
      await presetsShown();
      openRecentCustomRuns();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      // Each label is pinned whole: each window is listed once, so its range as stored alone.
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        '2026-09-01 – 2026-09-30',
        '2026-07-08 – 2026-10-05',
      ]);
      expect(Array.from(select.options, (option) => option.value)).not.toContain(
        String(failed.id),
      );
    });

    it('tells runs of the same window apart by a copy number from the oldest, and gives the chosen one’s id and finished instant in its hint', async () => {
      /** A succeeded ten-day custom run of one shared window, finished at `finishedAt`. */
      const sameWindow = (id: number, finishedAt: string): Run =>
        makeRun({
          id,
          kind: 'custom',
          trigger: 'manual',
          window_start: '2026-09-30',
          window_end: '2026-10-09',
          days: 10,
          display_timezone: 'America/New_York',
          finished_at: finishedAt,
        });
      const later = sameWindow(303, '2026-10-10T13:00:00Z');
      // Two runs finished within the same minute: their instants cannot tell them apart.
      const second = sameWindow(302, '2026-10-09T14:05:40Z');
      const first = sameWindow(301, '2026-10-09T14:05:10Z');
      const runs = [later, second, first] as const;
      stubApi({ 'GET /runs': json(200, runs) });
      await renderPicker();
      await presetsShown();
      openRecentCustomRuns();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      const labels = Array.from(select.options, (option) => option.textContent ?? '').slice(1);
      // Newest first as listed: the oldest is the range alone, each later one adds its number.
      expect(labels).toEqual([
        '2026-09-30 – 2026-10-09 (3)',
        '2026-09-30 – 2026-10-09 (2)',
        '2026-09-30 – 2026-10-09',
      ]);
      expect(labels).toEqual([
        reopenLabel(later, 3),
        reopenLabel(second, 2),
        reopenLabel(first, 1),
      ]);
      expect(new Set(labels).size).toBe(runs.length);
      runs.forEach((run, index) => {
        // The label is bounded: it never carries the run id or the finished instant.
        expect(labels[index]).not.toContain(String(run.id));
        expect(labels[index]).not.toContain('computed');

        fireEvent.change(select, { target: { value: String(run.id) } });
        const instant = formatInstant(run.finished_at, run.display_timezone);
        // The instant is shown in the run's display time zone, which the hint names.
        expect(instant.endsWith('America/New_York')).toBe(true);
        expect(accessibleDescription(select)).toBe(
          `Run ${String(run.id)} · 10 days · computed ${instant}`,
        );
      });
      expect(formatInstant(first.finished_at, first.display_timezone)).toBe(
        formatInstant(second.finished_at, second.display_timezone),
      );
    });

    it('adds a copy number only to the later listed runs of a shared window, counts no failed run, and names every chosen run in its hint', async () => {
      /** A later succeeded run of {@link september}'s window, with a four-digit id. */
      const septemberAgain = makeRun({
        id: 2031,
        kind: 'custom',
        trigger: 'manual',
        window_start: september.window_start,
        window_end: september.window_end,
        days: september.days,
      });
      /**
       * An older, failed run of {@link customRun}'s window: it is not listed, so it takes no
       * number, and customRun stays the oldest listed run of that window.
       */
      const failedSameWindow = makeRun({
        id: 201,
        kind: 'custom',
        trigger: 'manual',
        window_start: customRun.window_start,
        window_end: customRun.window_end,
        days: customRun.days,
        status: 'failed',
      });
      // The two runs of one window are not adjacent: they are numbered over the whole list.
      const listed = [septemberAgain, customRun, september] as const;
      stubApi({
        'GET /runs': json(200, [septemberAgain, customRun, september, failedSameWindow]),
      });
      await renderPicker();
      await presetsShown();
      openRecentCustomRuns();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      // The older run of the shared window is the range alone and the newer adds "(2)"; the
      // older failed run of customRun's window is not counted, so customRun is the range alone.
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        '2026-09-01 – 2026-09-30 (2)',
        '2026-07-08 – 2026-10-05',
        '2026-09-01 – 2026-09-30',
      ]);
      expect(Array.from(select.options, (option) => option.value)).toEqual([
        '',
        ...listed.map((run) => String(run.id)),
      ]);

      // Whatever its label, the hint names each chosen run in full.
      for (const run of listed) {
        fireEvent.change(select, { target: { value: String(run.id) } });
        const description = accessibleDescription(select) ?? '';
        expect(description.startsWith(`Run ${String(run.id)} · `)).toBe(true);
        expect(description).toBe(reopenHint(run));
      }
    });

    it('keeps each run’s copy number when runs of another window are listed between them', async () => {
      /** A succeeded custom run of {@link september}'s window with a five-digit id. */
      const ofSeptember = (id: number): Run =>
        makeRun({
          id,
          kind: 'custom',
          trigger: 'manual',
          window_start: september.window_start,
          window_end: september.window_end,
          days: september.days,
        });
      const a1 = ofSeptember(20312);
      const a2 = ofSeptember(20313);
      const a3 = ofSeptember(20315);
      /** The one listed run of another window, computed between a2 and a3. */
      const b1 = makeRun({
        id: 20314,
        kind: 'custom',
        trigger: 'manual',
        window_start: '2026-09-16',
        window_end: '2026-09-29',
        days: 14,
      });
      // Newest first, as the API lists them, with b1 between the runs of the shared window.
      const runs = [a3, b1, a2, a1] as const;
      stubApi({ 'GET /runs': json(200, runs) });
      await renderPicker();
      await presetsShown();
      openRecentCustomRuns();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      // The options keep the list's order; a1, the oldest of its window, is the range alone.
      expect(Array.from(select.options, (option) => option.value)).toEqual([
        '',
        ...runs.map((run) => String(run.id)),
      ]);
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        '2026-09-01 – 2026-09-30 (3)',
        '2026-09-16 – 2026-09-29',
        '2026-09-01 – 2026-09-30 (2)',
        '2026-09-01 – 2026-09-30',
      ]);
      expect(Array.from(select.options, (option) => option.textContent).slice(1)).toEqual([
        reopenLabel(a3, 3),
        reopenLabel(b1, 1),
        reopenLabel(a2, 2),
        reopenLabel(a1, 1),
      ]);

      // The hint names each chosen run by its id, which no label carries.
      for (const run of runs) {
        fireEvent.change(select, { target: { value: String(run.id) } });
        expect(accessibleDescription(select)).toBe(reopenHint(run));
      }
    });

    it('selects the custom run on screen, and opens nothing for the empty choice', async () => {
      stubApi({ 'GET /runs': json(200, [september, customRun]) });
      const { router } = await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();
      openRecentCustomRuns();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      expect(select.value).toBe(String(CUSTOM_RUN_ID));
      expect(reopenButton().hasAttribute('disabled')).toBe(false);

      fireEvent.change(select, { target: { value: '' } });

      expect(select.value).toBe('');
      expect(reopenButton().hasAttribute('disabled')).toBe(true);
      fireEvent.click(reopenButton());
      expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
    });

    it('describes the chosen run by its id, length and finished instant in a hint under the select, and the empty choice not at all', async () => {
      stubApi({ 'GET /runs': json(200, [september, customRun]) });
      await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();
      openRecentCustomRuns();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      // The run on screen is chosen, so its hint is the select's description.
      const onScreen = `Run ${String(CUSTOM_RUN_ID)} · 90 days · computed ${formatInstant(customRun.finished_at, customRun.display_timezone)}`;
      expect(reopenHint(customRun)).toBe(onScreen);
      expect(accessibleDescription(select)).toBe(onScreen);
      // The hint is visible text after the select, in the disclosed region, and is all the
      // select's description refers to.
      const hint = within(recentCustomRunsRegion()).getByText(onScreen);
      expect(select.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
      expect(select.getAttribute('aria-describedby')).toBe(hint.id);

      // The description follows a changed choice.
      fireEvent.change(select, { target: { value: String(september.id) } });
      expect(accessibleDescription(select)).toBe(
        `Run ${String(september.id)} · 30 days · computed ${formatInstant(september.finished_at, september.display_timezone)}`,
      );
      expect(within(recentCustomRunsRegion()).queryByText(onScreen)).toBeNull();

      // The empty choice names no run: no hint is rendered and the select has no description.
      fireEvent.change(select, { target: { value: '' } });
      expect(select.getAttribute('aria-describedby')).toBeNull();
      expect(accessibleDescription(select)).toBeNull();
      expect(within(recentCustomRunsRegion()).queryByText(/· computed /)).toBeNull();
    });

    it('says "No custom windows yet", with no select, when GET /runs lists no custom run', async () => {
      const api = stubApi({ 'GET /runs': json(200, []) });
      const { queryClient } = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();

      // Every read has answered, the empty custom-run list included.
      await waitFor(() => {
        expect(screen.getByText('No custom windows yet')).toBeTruthy();
      });
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      const listed = customRunReads(api)[0];
      expect(listed?.search.get('kind')).toBe('custom');
      expect(within(recentCustomRunsRegion()).getByText('No custom windows yet')).toBeTruthy();
      expect(screen.queryByRole('combobox', { name: 'Reopen a custom window' })).toBeNull();
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(screen.queryByText('Could not load recent custom windows')).toBeNull();
    });

    it('hides the select while no succeeded custom run exists', async () => {
      const api = stubApi({ 'GET /runs': json(200, [failed]) });
      const { queryClient } = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();

      await waitFor(() => {
        expect(customRunReads(api)).toHaveLength(1);
      });
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      expect(statusWithText('Loading windows')).toBeUndefined();
      expect(screen.getByText('No custom windows yet')).toBeTruthy();
      expect(screen.queryByRole('combobox', { name: 'Reopen a custom window' })).toBeNull();
      expect(screen.queryByText('Could not load recent custom windows')).toBeNull();
    });
  });

  describe('announcing the custom run on screen', () => {
    it('announces a custom run on screen in the picker’s hidden status region, never as an alert', async () => {
      stubApi();
      await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();

      const region = openedStatusRegion();
      expect(region.classList.contains('ds-visually-hidden')).toBe(true);
      // The message lands a moment after the region is committed, in that same region.
      await waitFor(() => {
        expect(region.textContent).toBe(openedMessage(customRun));
      });
      expect(openedStatusRegion()).toBe(region);
      expect(region.textContent).toBe(
        `Custom window 2026-07-08 – 2026-10-05 opened, 90 days, run ${String(CUSTOM_RUN_ID)}`,
      );
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('announces nothing for the default run', async () => {
      stubApi();
      const { queryClient } = await renderPicker();
      await presetsShown();
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      await openedMessageSettled();

      expect(openedStatusRegion().textContent).toBe('');
    });

    it('follows the run on screen: a new message for each custom run, none for the default', async () => {
      const september = makeRun({
        id: 203,
        kind: 'custom',
        trigger: 'manual',
        window_start: '2026-09-01',
        window_end: '2026-09-30',
        days: 30,
      });
      stubApi();
      const { router } = await renderRunFromUrl([customRun, september]);
      await presetsShown();
      const region = openedStatusRegion();
      await openedMessageSettled();
      expect(region.textContent).toBe('');

      await act(() => router.navigate({ to: '/actions', search: { run: customRun.id } }));
      await waitFor(() => {
        expect(region.textContent).toBe(openedMessage(customRun));
      });

      await act(() => router.navigate({ to: '/actions', search: { run: september.id } }));
      await waitFor(() => {
        expect(region.textContent).toBe(openedMessage(september));
      });
      // The same region throughout, so every message lands in a live region that exists.
      expect(openedStatusRegion()).toBe(region);

      await act(() => router.navigate({ to: '/actions', search: {} }));
      await waitFor(() => {
        expect(region.textContent).toBe('');
      });
      // Nothing is pending for the default run either, so the region stays empty.
      await openedMessageSettled();
      expect(region.textContent).toBe('');
      expect(openedStatusRegion()).toBe(region);
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });

  describe('the recent custom windows disclosure', () => {
    it('sits on the preset row right after "Custom range", outside the preset group, with its region below the picker', async () => {
      stubApi({ 'GET /runs': json(200, [customRun]) });
      await renderPicker();
      await presetsShown();

      const group = screen.getByRole('group', { name: 'Analysis window' });
      const toggle = button('Recent custom windows');
      const row = group.closest('.ds-daterange__row');
      expect(row).not.toBeNull();
      // On the same row as the presets, never inside their group.
      expect(toggle.closest('.ds-daterange__row')).toBe(row);
      expect(group.contains(toggle)).toBe(false);
      // Directly after the group, whose last button is "Custom range".
      expect(group.nextElementSibling).toBe(toggle);
      expect(within(group).getAllByRole('button').at(-1)).toBe(button('Custom range'));

      // The region it controls is outside the picker, after it, so opening it moves no control
      // of the preset row.
      const picker = group.closest('.ds-daterange');
      const region = recentCustomRunsRegion();
      expect(picker?.contains(region)).toBe(false);
      expect(
        (picker?.compareDocumentPosition(region) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).not.toBe(0);

      fireEvent.click(toggle);
      const select = await within(region).findByRole('combobox', {
        name: 'Reopen a custom window',
      });
      // Reopen follows the select, below it, inside the same region.
      const reopen = within(region).getByRole('button', { name: 'Reopen' });
      expect(
        select.compareDocumentPosition(reopen) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).not.toBe(0);
      expect(group.nextElementSibling).toBe(toggle);
    });

    it('starts closed and reads no GET /runs?kind=custom on mount', async () => {
      const api = stubApi({ 'GET /runs': json(200, [customRun]) });
      const { queryClient } = await renderPicker();
      await presetsShown();
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });

      const toggle = button('Recent custom windows');
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      // The controlled region is in place, hidden and empty, so aria-controls names an element.
      const region = recentCustomRunsRegion();
      expect(region.hidden).toBe(true);
      expect(region.childNodes).toHaveLength(0);
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(customRunReads(api)).toHaveLength(0);
      expect(api.calls.map((call) => `${call.method} ${call.path}`).sort()).toEqual([
        'GET /config',
        'GET /runs/latest',
      ]);
    });

    it('opens on a press: one GET /runs?kind=custom, a spinner until it answers, then the select', async () => {
      const list = deferred();
      const api = stubApi({ 'GET /runs': list.handler });
      await renderPicker();
      await presetsShown();

      const toggle = button('Recent custom windows');
      act(() => {
        toggle.focus();
      });
      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      // Pressing it leaves focus on it.
      expect(document.activeElement).toBe(toggle);
      await waitFor(() => {
        expect(customRunReads(api)).toHaveLength(1);
      });
      expect(customRunReads(api)[0]?.search.get('kind')).toBe('custom');
      expect(customRunReads(api)[0]?.search.get('limit')).toBe('20');
      const region = recentCustomRunsRegion();
      expect(region.hidden).toBe(false);
      expect(within(region).getByText('Loading recent custom windows')).toBeTruthy();
      expect(screen.queryByRole('combobox')).toBeNull();

      list.resolve(json(200, [customRun]));

      const select = await within(region).findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        reopenLabel(customRun, 1),
      ]);
      expect(within(region).queryByText('Loading recent custom windows')).toBeNull();
      expect(customRunReads(api)).toHaveLength(1);
    });

    it('closes on a second press, removing the select and reading nothing more', async () => {
      const api = stubApi({ 'GET /runs': json(200, [customRun]) });
      await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      await screen.findByRole('combobox', { name: 'Reopen a custom window' });

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(recentCustomRunsRegion().hidden).toBe(true);
      expect(customRunReads(api)).toHaveLength(1);
    });

    it('starts open, without a press, when the list was read earlier in the session', async () => {
      stubApi({ 'GET /runs': json(200, [customRun]) });
      const queryClient = createTestQueryClient();
      queryClient.setQueryData(queryKeys.runs.custom(20), [customRun]);
      await renderWithProviders(<WindowPicker run={makeRun()} />, { queryClient });
      await presetsShown();

      expect(button('Recent custom windows').getAttribute('aria-expanded')).toBe('true');
      // The cached list shows at once, with no spinner.
      expect(screen.getByRole('combobox', { name: 'Reopen a custom window' })).toBeTruthy();
      expect(statusWithText('Loading recent custom windows')).toBeUndefined();
    });

    it('forgets the list when closed, so a later picker starts closed and reads nothing until opened', async () => {
      const api = stubApi({ 'GET /runs': json(200, [customRun]) });
      const first = await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      await screen.findByRole('combobox', { name: 'Reopen a custom window' });
      fireEvent.click(toggle);
      expect(first.queryClient.getQueryData(queryKeys.runs.custom(20))).toBeUndefined();
      first.unmount();

      // Back on /actions later in the session, with the same cache.
      const { queryClient } = await renderWithProviders(<WindowPicker run={makeRun()} />, {
        queryClient: first.queryClient,
      });
      await presetsShown();
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });

      const again = button('Recent custom windows');
      expect(again.getAttribute('aria-expanded')).toBe('false');
      expect(recentCustomRunsRegion().hidden).toBe(true);
      expect(recentCustomRunsRegion().childNodes).toHaveLength(0);
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(customRunReads(api)).toHaveLength(1);

      act(() => {
        again.focus();
      });
      fireEvent.click(again);

      expect(again.getAttribute('aria-expanded')).toBe('true');
      expect(document.activeElement).toBe(again);
      // Forgotten, so the list is read afresh: a spinner until it answers, then the select.
      expect(within(recentCustomRunsRegion()).getByText('Loading recent custom windows')).toBeTruthy();
      await screen.findByRole('combobox', { name: 'Reopen a custom window' });
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      expect(customRunReads(api)).toHaveLength(2);
      expect(customRunReads(api)[1]?.search.get('kind')).toBe('custom');
    });

    it('reads the list once more when reopened after a close on the same picker', async () => {
      const api = stubApi({ 'GET /runs': json(200, [customRun]) });
      const { queryClient } = await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      await screen.findByRole('combobox', { name: 'Reopen a custom window' });
      fireEvent.click(toggle);
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(customRunReads(api)).toHaveLength(1);

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      // The closed list was forgotten: nothing to show until the new read answers.
      expect(within(recentCustomRunsRegion()).getByText('Loading recent custom windows')).toBeTruthy();
      expect(screen.queryByRole('combobox')).toBeNull();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        reopenLabel(customRun, 1),
      ]);
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      expect(customRunReads(api)).toHaveLength(2);
    });

    it('stays open on a later picker after a run is chosen, with that run selected', async () => {
      stubApi({ 'GET /runs': json(200, [customRun]) });
      const first = await renderPicker();
      await presetsShown();
      openRecentCustomRuns();
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });

      fireEvent.change(select, { target: { value: String(CUSTOM_RUN_ID) } });
      fireEvent.click(reopenButton());

      await waitFor(() => {
        expect(first.router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      });
      first.unmount();

      // The chosen run's page mounts a new picker with the same cache.
      await renderWithProviders(<WindowPicker run={customRun} />, {
        queryClient: first.queryClient,
        path: `/actions?run=${String(CUSTOM_RUN_ID)}`,
      });
      await presetsShown();

      expect(button('Recent custom windows').getAttribute('aria-expanded')).toBe('true');
      const reopened = screen.getByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      expect(reopened.value).toBe(String(CUSTOM_RUN_ID));
      expect(statusWithText('Loading recent custom windows')).toBeUndefined();
    });

    it('lists a computed run while open, and reads no list for one computed while closed', async () => {
      const api = stubApi({ 'GET /runs': json(200, []) });
      const { queryClient } = await renderPicker();
      await presetsShown();
      const toggle = openRecentCustomRuns();
      await screen.findByText('No custom windows yet');

      api.use({ 'GET /runs': json(200, [customRun]) });
      fireEvent.click(button('90 days'));

      // The created run invalidates the open list, which is read again and now lists it.
      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        reopenLabel(customRun, 1),
      ]);
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      expect(posts(api)).toHaveLength(1);
      expect(customRunReads(api)).toHaveLength(2);

      fireEvent.click(toggle);
      fireEvent.click(button('56 days'));

      await waitFor(() => {
        expect(posts(api)).toHaveLength(2);
      });
      await waitFor(() => {
        expect(toggle.hasAttribute('disabled')).toBe(false);
      });
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      // A closed list was forgotten: the invalidation reads nothing and opens nothing.
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(customRunReads(api)).toHaveLength(2);
      expect(queryClient.getQueryData(queryKeys.runs.custom(20))).toBeUndefined();
    });

    it('is disabled while a run computes, like every other control', async () => {
      const created = deferred();
      const api = stubApi({ 'POST /runs': created.handler });
      await renderPicker();
      await presetsShown();
      const toggle = button('Recent custom windows');
      expect(toggle.hasAttribute('disabled')).toBe(false);

      fireEvent.click(button('56 days'));
      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      await waitFor(() => {
        expect(toggle.hasAttribute('disabled')).toBe(true);
      });
      // A disabled button fires no click: the list stays closed and unread.
      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(customRunReads(api)).toHaveLength(0);

      created.resolve(apiError(409, 'run_in_progress', 'An analysis is already running.'));
      await waitFor(() => {
        expect(toggle.hasAttribute('disabled')).toBe(false);
      });
    });
  });
});
