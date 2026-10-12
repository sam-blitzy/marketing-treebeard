/**
 * Component tests for `CustomRunBanner`, the custom-window notice of `/actions?run=<id>`
 * (AAP 0.5.5, Window picker): "Custom window {start} – {end}" with the window's length and a
 * "Back to default" link to `/actions` without a `run` search param. Also for the older-default
 * notice: `isOlderDefaultRun`, which orders default runs as the backend's `latest_default_run`
 * does (`finished_at` newest first, a missing instant last, then the higher id), and
 * `OlderDefaultRunBanner`, "Older default run {start} – {end}" with "Back to default".
 *
 * The banner renders inside the test router of `../../test/render`, so the link's target and the
 * navigation it performs are read from `router.state.location`. It reads no server state, so no
 * request is stubbed.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { CUSTOM_RUN_ID, DEFAULT_RUN_ID, makeRun } from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import { byTextContent } from '../../test/text';
import { CustomRunBanner, OlderDefaultRunBanner, isOlderDefaultRun } from './CustomRunBanner';

/** The id of an older default run, opened at `/actions?run=6` after later default runs. */
const OLDER_DEFAULT_RUN_ID = 6;

/** The 90-day custom run that `POST /runs` returns for the 90-day preset of the fixture window. */
const customRun = makeRun({
  id: CUSTOM_RUN_ID,
  kind: 'custom',
  trigger: 'manual',
  window_start: '2026-07-08',
  window_end: '2026-10-05',
  days: 90,
});

/** The banner element, found by its in-page id. */
function customBanner(container: HTMLElement): HTMLElement {
  const banner = container.querySelector<HTMLElement>('#banner-custom-window');
  if (banner === null) {
    throw new Error('The custom-window banner is not rendered.');
  }
  return banner;
}

describe('CustomRunBanner', () => {
  it('names the custom window with its dates as stored and its length', async () => {
    const { container } = await renderWithProviders(<CustomRunBanner run={customRun} />, {
      path: `/actions?run=${String(CUSTOM_RUN_ID)}`,
    });

    const banner = customBanner(container);
    expect(
      within(banner).getByText(
        byTextContent('.ds-banner__title', 'Custom window 2026-07-08 – 2026-10-05'),
      ),
    ).toBeTruthy();
    expect(within(banner).getByText('90 days')).toBeTruthy();
    // Plain content: only the open-discrepancies banner is an alert (AAP 0.5.5).
    expect(banner.getAttribute('role')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(banner.className).toContain('ds-banner--info');
  });

  it('keeps the singular for a one-day window', async () => {
    const oneDay = makeRun({
      id: CUSTOM_RUN_ID,
      kind: 'custom',
      window_start: '2026-10-05',
      window_end: '2026-10-05',
      days: 1,
    });
    const { container } = await renderWithProviders(<CustomRunBanner run={oneDay} />, {
      path: `/actions?run=${String(CUSTOM_RUN_ID)}`,
    });

    const banner = customBanner(container);
    expect(
      within(banner).getByText(
        byTextContent('.ds-banner__title', 'Custom window 2026-10-05 – 2026-10-05'),
      ),
    ).toBeTruthy();
    expect(within(banner).getByText('1 day')).toBeTruthy();
    expect(within(banner).queryByText('1 days')).toBeNull();
  });

  it('links back to the default run at /actions with every search param cleared', async () => {
    const { router } = await renderWithProviders(<CustomRunBanner run={customRun} />, {
      path: `/actions?run=${String(CUSTOM_RUN_ID)}`,
    });
    expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });

    const link = screen.getByRole('link', { name: 'Back to default' });
    expect(link.getAttribute('href')).toBe('/actions');
    // Exact matching: the link leads to another view, so it is not the current page.
    expect(link.getAttribute('aria-current')).toBeNull();

    fireEvent.click(link);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/actions');
      expect(router.state.location.search).toEqual({});
    });
    expect(router.state.location.href).toBe('/actions');
  });

  it('renders nothing for a default run', async () => {
    const { container } = await renderWithProviders(<CustomRunBanner run={makeRun()} />);

    expect(container.querySelector('#banner-custom-window')).toBeNull();
    expect(screen.queryByText(/Custom window/u)).toBeNull();
    expect(screen.queryByRole('link', { name: 'Back to default' })).toBeNull();
  });
});

/** The latest succeeded default run, as `GET /runs/latest` returns it. */
const latestDefault = makeRun({
  id: DEFAULT_RUN_ID,
  finished_at: '2026-10-06T06:00:00Z',
});

/** A default run that finished a week before {@link latestDefault}, opened by id. */
const olderDefault = makeRun({
  id: OLDER_DEFAULT_RUN_ID,
  window_start: '2026-09-01',
  window_end: '2026-09-28',
  days: 28,
  finished_at: '2026-09-29T06:00:00Z',
});

describe('isOlderDefaultRun', () => {
  it('is false for the latest default run itself', () => {
    expect(isOlderDefaultRun(latestDefault, latestDefault)).toBe(false);
    // The same id is the same run, whatever the copies say.
    expect(isOlderDefaultRun({ ...latestDefault, finished_at: null }, latestDefault)).toBe(false);
  });

  it('is true for a default run that finished before the latest one', () => {
    expect(isOlderDefaultRun(olderDefault, latestDefault)).toBe(true);
  });

  it('compares instants, not text: an offset that ends earlier is still older', () => {
    // 08:00 at +03:00 is 05:00 UTC, an hour before the latest run's 06:00 UTC, although its text
    // sorts after it.
    const offset = { ...olderDefault, finished_at: '2026-10-06T08:00:00+03:00' };
    expect(isOlderDefaultRun(offset, latestDefault)).toBe(true);
  });

  it('is false for a default run that finished after the latest one the client holds', () => {
    const newer = { ...olderDefault, finished_at: '2026-10-07T06:00:00Z' };
    expect(isOlderDefaultRun(newer, latestDefault)).toBe(false);
  });

  it('sorts a missing or unparseable finished_at last, as the oldest', () => {
    expect(isOlderDefaultRun({ ...olderDefault, finished_at: null }, latestDefault)).toBe(true);
    expect(isOlderDefaultRun({ ...olderDefault, finished_at: 'not an instant' }, latestDefault)).toBe(
      true,
    );
    // A latest run without an instant sorts after one that has it.
    const unfinishedLatest = { ...latestDefault, finished_at: null };
    expect(isOlderDefaultRun(olderDefault, unfinishedLatest)).toBe(false);
  });

  it('falls back to the id when the instants are equal or both missing', () => {
    const tie = { ...olderDefault, finished_at: latestDefault.finished_at };
    // DEFAULT_RUN_ID (101) is higher than OLDER_DEFAULT_RUN_ID (6): the latest comes first.
    expect(isOlderDefaultRun(tie, latestDefault)).toBe(true);
    expect(isOlderDefaultRun(latestDefault, { ...tie, id: DEFAULT_RUN_ID + 1 })).toBe(true);
    expect(isOlderDefaultRun({ ...tie, id: DEFAULT_RUN_ID + 1 }, latestDefault)).toBe(false);
    expect(
      isOlderDefaultRun(
        { ...olderDefault, finished_at: null },
        { ...latestDefault, finished_at: null },
      ),
    ).toBe(true);
  });

  it('is false for a custom run, which has its own banner', () => {
    const custom = { ...olderDefault, kind: 'custom' as const };
    expect(isOlderDefaultRun(custom, latestDefault)).toBe(false);
  });
});

describe('OlderDefaultRunBanner', () => {
  it('names the older default run with its dates and length, and says it is not the latest', async () => {
    const { container } = await renderWithProviders(<OlderDefaultRunBanner run={olderDefault} />, {
      path: `/actions?run=${String(OLDER_DEFAULT_RUN_ID)}`,
    });

    const banner = container.querySelector<HTMLElement>('#banner-older-default-run');
    if (banner === null) {
      throw new Error('The older-default-run banner is not rendered.');
    }
    expect(
      within(banner).getByText(
        byTextContent('.ds-banner__title', 'Older default run 2026-09-01 – 2026-09-28'),
      ),
    ).toBeTruthy();
    expect(within(banner).getByText('28 days · not the latest default run')).toBeTruthy();
    // Plain content: only the open-discrepancies banner is an alert (AAP 0.5.5).
    expect(banner.getAttribute('role')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(banner.className).toContain('ds-banner--info');
  });

  it('links back to the latest default run at /actions with every search param cleared', async () => {
    const { router } = await renderWithProviders(<OlderDefaultRunBanner run={olderDefault} />, {
      path: `/actions?run=${String(OLDER_DEFAULT_RUN_ID)}`,
    });
    expect(router.state.location.search).toEqual({ run: OLDER_DEFAULT_RUN_ID });

    const link = screen.getByRole('link', { name: 'Back to default' });
    expect(link.getAttribute('href')).toBe('/actions');
    expect(link.getAttribute('aria-current')).toBeNull();

    fireEvent.click(link);

    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(router.state.location.href).toBe('/actions');
  });

  it('renders nothing for a custom run', async () => {
    const { container } = await renderWithProviders(
      <OlderDefaultRunBanner run={customRun} />,
    );

    expect(container.querySelector('#banner-older-default-run')).toBeNull();
    expect(screen.queryByText(/Older default run/u)).toBeNull();
  });
});
