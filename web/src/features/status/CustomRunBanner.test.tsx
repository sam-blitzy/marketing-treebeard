/**
 * Component tests for `CustomRunBanner`, the custom-window notice of `/actions?run=<id>`
 * (AAP 0.5.5, Window picker): "Custom window {start} – {end}" with the window's length and a
 * "Back to default" link to `/actions` without a `run` search param.
 *
 * The banner renders inside the test router of `../../test/render`, so the link's target and the
 * navigation it performs are read from `router.state.location`. It reads no server state, so no
 * request is stubbed.
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { CUSTOM_RUN_ID, makeRun } from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import { CustomRunBanner } from './CustomRunBanner';

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
    expect(within(banner).getByText('Custom window 2026-07-08 – 2026-10-05')).toBeTruthy();
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
    expect(within(banner).getByText('Custom window 2026-10-05 – 2026-10-05')).toBeTruthy();
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
