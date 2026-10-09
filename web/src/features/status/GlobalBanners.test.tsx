/**
 * Component tests for `GlobalBanners`, the global banners of `/actions` (AAP 0.5.5), and for
 * its ordering helper `orderBanners`.
 *
 * The banners render in the fixed order open discrepancies, tracking findings, simulated
 * sources, stale then incomplete datasets, and the short window, whatever order the API sends
 * them in. The open-discrepancies banner reports the live `Overlay.open_discrepancies` count,
 * links to the Campaigns discrepancy group and is the only banner with `role="alert"`; every
 * other banner shows the API's own message. Payloads come from `../../test/fixtures`.
 *
 * The component is presentational and calls no data hook, so it renders without providers and
 * no request is stubbed.
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { Banner, BannerTone } from '../../api/types';
import { FIXTURE_BANNERS, makeBanner, makeOverlay } from '../../test/fixtures';
import { BANNER_ORDER, GlobalBanners, orderBanners } from './GlobalBanners';

/** Fresh copies of the five fixture banners, in the API's order. */
function fixtureBanners(): Banner[] {
  return FIXTURE_BANNERS.map((banner) => ({ ...banner }));
}

/** The stale-datasets banner, which the default fixture page does not carry. */
const staleBanner = makeBanner({
  code: 'stale_datasets',
  tone: 'warning',
  message: 'Data as of 2026-10-04: google.metrics_daily last succeeded more than 24 hours ago',
  count: 1,
});

/** A fixture banner by code. */
function fixtureBanner(code: string): Banner {
  const found = FIXTURE_BANNERS.find((banner) => banner.code === code);
  if (found === undefined) {
    throw new Error(`No fixture banner has the code ${code}.`);
  }
  return { ...found };
}

/** The title paragraph of every rendered banner, in document order. */
function bannerTitles(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.ds-banner__title'), (title) =>
    (title.textContent ?? '').trim(),
  );
}

/** The rendered banner whose title is `title`. */
function bannerTitled(title: string): HTMLElement {
  const banner = screen.getByText(title).closest<HTMLElement>('.ds-banner');
  if (banner === null) {
    throw new Error(`No banner is titled "${title}".`);
  }
  return banner;
}

describe('BANNER_ORDER', () => {
  it('lists the API banner codes in the fixed order of AAP 0.5.5, frozen', () => {
    expect(BANNER_ORDER).toEqual([
      'open_discrepancies',
      'tracking_absent',
      'simulated_sources',
      'stale_datasets',
      'incomplete_datasets',
      'short_window',
    ]);
    expect(Object.isFrozen(BANNER_ORDER)).toBe(true);
  });
});

describe('orderBanners', () => {
  it('re-applies the fixed order to banners that arrive out of order', () => {
    const [discrepancies, tracking, simulated, incomplete, shortWindow] = fixtureBanners();
    const shuffled = [shortWindow, incomplete, simulated, staleBanner, tracking, discrepancies];

    expect(orderBanners(shuffled.filter((banner) => banner !== undefined))).toEqual([
      discrepancies,
      tracking,
      simulated,
      staleBanner,
      incomplete,
      shortWindow,
    ]);
  });

  it('keeps unknown codes after the known ones in their API order, and never mutates its input', () => {
    const futureA = makeBanner({ code: 'future_a', tone: 'info', message: 'A', count: null });
    const futureB = makeBanner({ code: 'future_b', tone: 'info', message: 'B', count: null });
    const shortWindow = fixtureBanner('short_window');
    const discrepancies = fixtureBanner('open_discrepancies');
    const input = [futureA, shortWindow, futureB, discrepancies];
    const snapshot = [...input];

    const ordered = orderBanners(input);

    expect(ordered).toEqual([discrepancies, shortWindow, futureA, futureB]);
    expect(ordered).not.toBe(input);
    expect(input).toEqual(snapshot);
    // The same banner objects, not copies.
    expect(ordered[0]).toBe(discrepancies);
  });

  it('is stable for banners that share a position', () => {
    const first = makeBanner({ code: 'tracking_absent', tone: 'warning', message: 'first' });
    const second = makeBanner({ code: 'tracking_absent', tone: 'warning', message: 'second' });

    expect(orderBanners([first, second]).map((banner) => banner.message)).toEqual([
      'first',
      'second',
    ]);
  });
});

describe('GlobalBanners', () => {
  it('renders every banner in the fixed order, the discrepancy banner above everything', () => {
    const [discrepancies, tracking, simulated, incomplete, shortWindow] = fixtureBanners();
    const shuffled = [shortWindow, simulated, incomplete, staleBanner, tracking, discrepancies];
    const { container } = render(
      <GlobalBanners
        banners={shuffled.filter((banner) => banner !== undefined)}
        overlay={makeOverlay({ open_discrepancies: 3 })}
      />,
    );

    expect(bannerTitles(container)).toEqual([
      '3 open discrepancies to resolve first',
      'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
      'Simulated data: Google (credentials not configured); LinkedIn (credentials not configured); HubSpot (credentials not configured)',
      'Data as of 2026-10-04: google.metrics_daily last succeeded more than 24 hours ago',
      'Incomplete data: google.search_terms (result limit reached at finest partition)',
      'Window of 10 days is below the 14-day trusted minimum',
    ]);
  });

  it('titles the discrepancy banner with the live overlay count, links to the discrepancy group and is the only alert', () => {
    render(
      <GlobalBanners banners={fixtureBanners()} overlay={makeOverlay({ open_discrepancies: 3 })} />,
    );

    const alerts = screen.getAllByRole('alert');
    expect(alerts).toHaveLength(1);
    const [alert] = alerts;
    if (alert === undefined) {
      throw new Error('No alert is rendered.');
    }
    expect(alert.id).toBe('banner-discrepancies');
    expect(alert.className).toContain('ds-banner--critical');
    // The title reports the overlay's live count, not the run's stored banner count (1).
    expect(within(alert).getByText('3 open discrepancies to resolve first')).toBeTruthy();
    // The API's message is the supporting content.
    expect(
      within(alert).getByText('1 discrepancy to resolve before acting on other recommendations'),
    ).toBeTruthy();
    const link = within(alert).getByRole('link', { name: 'Review discrepancies' });
    expect(link.getAttribute('href')).toBe('#group-campaigns-discrepancies');

    // Every other banner is silent content.
    const shortWindow = bannerTitled('Window of 10 days is below the 14-day trusted minimum');
    expect(shortWindow.getAttribute('role')).toBeNull();
    expect(shortWindow.className).toContain('ds-banner--warning');
    expect(within(shortWindow).queryByRole('link')).toBeNull();
  });

  it('uses the singular for one open discrepancy and groups large counts', () => {
    const { rerender } = render(
      <GlobalBanners banners={[makeBanner()]} overlay={makeOverlay({ open_discrepancies: 1 })} />,
    );
    expect(screen.getByText('1 open discrepancy to resolve first')).toBeTruthy();

    rerender(
      <GlobalBanners banners={[makeBanner()]} overlay={makeOverlay({ open_discrepancies: 1204 })} />,
    );
    expect(screen.getByText('1,204 open discrepancies to resolve first')).toBeTruthy();
  });

  it.each([
    ['zero', 0],
    ['a negative count', -2],
    ['a fractional count', 1.5],
  ])('hides the discrepancy banner for %s, and keeps the other banners', (_label, count) => {
    const { container } = render(
      <GlobalBanners
        banners={fixtureBanners()}
        overlay={makeOverlay({ open_discrepancies: count })}
      />,
    );

    expect(screen.queryByRole('alert')).toBeNull();
    expect(container.querySelector('#banner-discrepancies')).toBeNull();
    expect(screen.queryByText(/open discrepanc/u)).toBeNull();
    expect(bannerTitles(container)).toHaveLength(4);
    expect(bannerTitles(container)[0]).toBe(
      'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
    );
  });

  it('shows the discrepancy banner for a discrepancy the overlay counts after the run', () => {
    // The run's banner carried no message worth showing; the live count still raises it.
    render(
      <GlobalBanners
        banners={[makeBanner({ message: '   ' })]}
        overlay={makeOverlay({ open_discrepancies: 2 })}
      />,
    );

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('2 open discrepancies to resolve first')).toBeTruthy();
    expect(alert.querySelector('.ds-banner__content')).toBeNull();
    expect(within(alert).getByRole('link', { name: 'Review discrepancies' })).toBeTruthy();
  });

  it('shows the discrepancy banner at most once', () => {
    render(
      <GlobalBanners
        banners={[makeBanner(), makeBanner({ message: 'A repeated discrepancy banner' })]}
        overlay={makeOverlay({ open_discrepancies: 2 })}
      />,
    );

    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.queryByText('A repeated discrepancy banner')).toBeNull();
  });

  it('skips a banner with a blank or missing message, and renders nothing when none is left', () => {
    const missing = {
      ...makeBanner({ code: 'tracking_absent', tone: 'warning' }),
      message: null,
    } as unknown as Banner;
    const { container } = render(
      <GlobalBanners
        banners={[
          makeBanner({ code: 'short_window', tone: 'warning', message: '  ' }),
          missing,
          makeBanner(),
        ]}
        overlay={makeOverlay({ open_discrepancies: 0 })}
      />,
    );

    expect(container.innerHTML).toBe('');
    expect(screen.queryByText('null')).toBeNull();
  });

  it('renders nothing for an empty banner list', () => {
    const { container } = render(<GlobalBanners banners={[]} overlay={makeOverlay()} />);

    expect(container.innerHTML).toBe('');
  });

  it('trims messages, renders an unknown tone as neutral and keeps an unknown code last', () => {
    const consoleError = vi.spyOn(console, 'error');
    const future = makeBanner({
      code: 'future_banner',
      tone: 'emphatic' as BannerTone,
      message: '  A banner code the client does not know yet  ',
      count: null,
    });
    const tracking = fixtureBanner('tracking_absent');
    const trackingAgain = { ...tracking, message: 'A second tracking finding' };
    const { container } = render(
      <GlobalBanners
        banners={[future, tracking, trackingAgain]}
        overlay={makeOverlay({ open_discrepancies: 0 })}
      />,
    );

    expect(bannerTitles(container)).toEqual([
      'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
      'A second tracking finding',
      'A banner code the client does not know yet',
    ]);
    const unknown = bannerTitled('A banner code the client does not know yet');
    expect(unknown.className).toContain('ds-banner--neutral');
    expect(unknown.className).not.toContain('emphatic');
    // A repeated code gets a key of its own, so React reports no duplicate key.
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});
