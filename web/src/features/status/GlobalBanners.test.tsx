/**
 * Component tests for `GlobalBanners`, the global banners of `/actions` (AAP 0.5.5), and for
 * its ordering helper `orderBanners`.
 *
 * The banners render in the fixed order open discrepancies, tracking findings, simulated
 * sources, stale then incomplete datasets, and the short window, whatever order the API sends
 * them in. The open-discrepancies banner reports the live `Overlay.open_discrepancies` count,
 * links to the Campaigns discrepancy group and is the only banner with `role="alert"`; every
 * other banner shows the API's own message. Payloads come from `../../test/fixtures`. A run
 * notice the page passes (`runNotice`) renders in the same stack, right after the discrepancy
 * banner or first without it, and keeps the component rendering when no banner is left.
 *
 * The fixed order is checked against {@link AAP_BANNER_ORDER}, the six known banner codes
 * written out here in that order, rather than against the component's own `BANNER_ORDER`: an
 * expectation read from the constant under test passes whatever order the constant holds, so
 * it cannot catch a reorder. The written-out codes stay checked against the `BannerCode` union
 * in `types.ts`, the client's hand mirror of the API's codes, so a code renamed or dropped there
 * fails the typecheck; a backend rename or removal reaches this check only once that mirror is
 * updated. `BANNER_ORDER`, the mirror of the backend's `analysis.context.BANNER_ORDER`, must
 * equal the oracle. The fixture banners carry no stale-datasets banner, so the stale slot uses a
 * local one worded as the backend words it. The codes the client must not know, the "later"
 * codes of the unknown-code cases, are literals too.
 *
 * The component is presentational and calls no data hook, so it renders without providers and
 * no request is stubbed.
 */
import { render, screen, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';

import type { Banner, BannerCode, BannerTone } from '../../api/types';
import { Banner as DesignBanner } from '../../design-system';
import { FIXTURE_BANNERS, makeBanner, makeOverlay } from '../../test/fixtures';
import { BANNER_ORDER, GlobalBanners, orderBanners } from './GlobalBanners';

/**
 * The fixed order of the global banners as AAP 0.5.5 sets it: open discrepancies, tracking
 * findings, simulated sources, stale then incomplete datasets (its single "stale or incomplete"
 * slot, stale first, as the backend builds them), and the short window.
 *
 * Written out here deliberately, independently of the component's {@link BANNER_ORDER}: an
 * expectation derived from the constant under test cannot catch a reorder of it. `satisfies`
 * checks each literal against `types.ts`' hand-mirrored `BannerCode` union, so a code renamed or
 * dropped there fails the typecheck; a backend rename or removal reaches this check only once
 * that mirror is updated.
 */
const AAP_BANNER_ORDER = Object.freeze([
  'open_discrepancies',
  'tracking_absent',
  'simulated_sources',
  'stale_datasets',
  'incomplete_datasets',
  'short_window',
] as const satisfies readonly BannerCode[]);

/**
 * A fresh stale-datasets banner, worded as `analysis/overlay.py` builds it: "Stale data:
 * {dataset} (last success {instant})", the instant in the run's `display_timezone` as
 * `formatInstant` renders it, with the warning tone and the count of stale datasets. The
 * fixture banners carry none, so this banner fills the stale slot of the order.
 */
function staleBanner(): Banner {
  return {
    code: 'stale_datasets' satisfies BannerCode,
    tone: 'warning',
    message: 'Stale data: google.metrics_daily (last success Oct 4, 2026, 5:31 AM UTC)',
    count: 1,
  } satisfies Banner;
}

/** Fresh copies of the five fixture banners, in the API's order. */
function fixtureBanners(): Banner[] {
  return FIXTURE_BANNERS.map((banner) => ({ ...banner }));
}

/** The five fixture banners, each a fresh copy, named by its place in the API's order. */
interface NamedFixtureBanners {
  discrepancies: Banner;
  tracking: Banner;
  simulated: Banner;
  incomplete: Banner;
  shortWindow: Banner;
}

/**
 * Fresh copies of the fixture banners by name: open discrepancies, tracking absent, simulated
 * sources, incomplete datasets and short window, the order `FIXTURE_BANNERS` documents.
 *
 * @throws Error when `FIXTURE_BANNERS` holds fewer than five banners.
 */
function namedFixtureBanners(): NamedFixtureBanners {
  const [discrepancies, tracking, simulated, incomplete, shortWindow] = fixtureBanners();
  if (
    discrepancies === undefined ||
    tracking === undefined ||
    simulated === undefined ||
    incomplete === undefined ||
    shortWindow === undefined
  ) {
    throw new Error('FIXTURE_BANNERS holds fewer than five banners.');
  }
  return { discrepancies, tracking, simulated, incomplete, shortWindow };
}

/** The message {@link orderedBanners} gives the banner of `code`: distinct for every code. */
function messageFor(code: BannerCode): string {
  return `Banner message for ${code}`;
}

/**
 * One banner per {@link AAP_BANNER_ORDER} code, in that order, each built by `makeBanner` with
 * the distinct message {@link messageFor} its code.
 */
function orderedBanners(): Banner[] {
  return AAP_BANNER_ORDER.map((code) =>
    makeBanner({ code, message: messageFor(code), count: null }),
  );
}

/** The title of the run notice {@link runNotice} builds. */
const RUN_NOTICE_TITLE = 'Custom window 2026-07-08 – 2026-10-05';

/** The element id of the run notice {@link runNotice} builds. */
const RUN_NOTICE_ID = 'banner-run-notice';

/**
 * A run notice as the page passes one: a plain design-system `Banner` with tone `info` and no
 * alert, standing in for the custom-window banner, which needs a router to render its link.
 */
function runNotice(): ReactElement {
  return <DesignBanner tone="info" id={RUN_NOTICE_ID} title={RUN_NOTICE_TITLE} />;
}

/** The rendered run notice, found by its element id. */
function noticeElement(container: HTMLElement): HTMLElement {
  const notice = container.querySelector<HTMLElement>(`#${RUN_NOTICE_ID}`);
  if (notice === null) {
    throw new Error('The run notice is not rendered.');
  }
  return notice;
}

/** The title paragraph of every rendered banner, in document order. */
function bannerTitles(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.ds-banner__title'), (title) =>
    (title.textContent ?? '').trim(),
  );
}

/**
 * The rendered banner that shows `text`, as its title or, for the discrepancy banner, as its
 * supporting content.
 */
function bannerShowing(text: string): HTMLElement {
  const banner = screen.getByText(text).closest<HTMLElement>('.ds-banner');
  if (banner === null) {
    throw new Error(`No banner shows "${text}".`);
  }
  return banner;
}

describe('BANNER_ORDER', () => {
  it('starts with the discrepancy banner, lists each code once and is frozen', () => {
    // The fixtures' default banner is the open-discrepancies banner, which sits above everything.
    expect(BANNER_ORDER[0]).toBe(makeBanner().code);
    expect(new Set(BANNER_ORDER).size).toBe(BANNER_ORDER.length);
    expect(Object.isFrozen(BANNER_ORDER)).toBe(true);
  });

  it('equals the fixed order written out independently of the component', () => {
    expect(BANNER_ORDER).toEqual(AAP_BANNER_ORDER);
  });

  it("agrees with the order of the fixture banners, which mirror the backend's", () => {
    const positions = FIXTURE_BANNERS.map((banner) =>
      (AAP_BANNER_ORDER as readonly string[]).indexOf(banner.code),
    );

    expect(positions).not.toContain(-1);
    expect([...positions].sort((left, right) => left - right)).toEqual(positions);
  });
});

describe('orderBanners', () => {
  it('re-applies the fixed order to banners that arrive out of order', () => {
    const inOrder = orderedBanners();

    const ordered = orderBanners([...inOrder].reverse());

    expect(ordered.map((banner) => banner.code)).toEqual([...AAP_BANNER_ORDER]);
    // The same banner objects, not copies.
    expect(ordered.every((banner, index) => banner === inOrder[index])).toBe(true);
  });

  it('puts the stale-datasets banner before the incomplete-datasets banner', () => {
    const { incomplete } = namedFixtureBanners();
    const stale = staleBanner();

    const ordered = orderBanners([incomplete, stale]);

    expect(ordered).toHaveLength(2);
    // The same banner objects, not copies.
    expect(ordered[0]).toBe(stale);
    expect(ordered[1]).toBe(incomplete);
  });

  it('keeps unknown codes after the known ones in their API order, and never mutates its input', () => {
    const futureA = makeBanner({ code: 'future_a', tone: 'info', message: 'A', count: null });
    const futureB = makeBanner({ code: 'future_b', tone: 'info', message: 'B', count: null });
    const { discrepancies, shortWindow } = namedFixtureBanners();
    const input = [futureA, shortWindow, futureB, discrepancies];
    // Copies of every banner too, so a change to a banner object is caught as well as a reorder.
    const snapshot = input.map((banner) => ({ ...banner }));

    const ordered = orderBanners(input);

    expect(ordered).toEqual([discrepancies, shortWindow, futureA, futureB]);
    expect(ordered).not.toBe(input);
    expect(input).toEqual(snapshot);
    // The same banner objects, not copies.
    expect(ordered[0]).toBe(discrepancies);
  });

  it('is stable for banners that share a position', () => {
    const { tracking } = namedFixtureBanners();
    const first = { ...tracking, message: 'first' };
    const second = { ...tracking, message: 'second' };

    expect(orderBanners([first, second]).map((banner) => banner.message)).toEqual([
      'first',
      'second',
    ]);
  });
});

describe('GlobalBanners', () => {
  it('renders the six known banners in the fixed order, whatever the input order', () => {
    const { container } = render(
      <GlobalBanners
        banners={orderedBanners().reverse()}
        overlay={makeOverlay({ open_discrepancies: 2 })}
      />,
    );

    // Written out in the fixed order: the discrepancy title reports the live count, and every
    // other title is its banner's own message.
    expect(bannerTitles(container)).toEqual([
      '2 open discrepancies to resolve first',
      messageFor('tracking_absent'),
      messageFor('simulated_sources'),
      messageFor('stale_datasets'),
      messageFor('incomplete_datasets'),
      messageFor('short_window'),
    ]);
    // The discrepancy banner is the only alert, rendered first, and every other banner follows it.
    const alerts = screen.getAllByRole('alert');
    expect(alerts).toHaveLength(1);
    const [alert] = alerts;
    if (alert === undefined) {
      throw new Error('No alert is rendered.');
    }
    const rendered = Array.from(container.querySelectorAll<HTMLElement>('.ds-banner'));
    expect(rendered).toHaveLength(AAP_BANNER_ORDER.length);
    expect(rendered[0]).toBe(alert);
    expect(within(alert).getByText(messageFor('open_discrepancies'))).toBeTruthy();
    for (const banner of rendered.slice(1)) {
      expect(alert.compareDocumentPosition(banner) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }
  });

  it('renders banners with unknown codes after every known banner, in their input order', () => {
    // Named so that their alphabetical order is the reverse of their input order.
    const sentFirst = makeBanner({
      code: 'zeta_later_banner',
      tone: 'info',
      message: 'A banner code added later, sent first',
      count: null,
    });
    const sentLast = makeBanner({
      code: 'alpha_later_banner',
      tone: 'info',
      message: 'Another banner code added later, sent last',
      count: null,
    });
    const { container } = render(
      <GlobalBanners
        banners={[sentFirst, ...orderedBanners().reverse(), sentLast]}
        overlay={makeOverlay({ open_discrepancies: 2 })}
      />,
    );

    const titles = bannerTitles(container);
    expect(titles).toHaveLength(AAP_BANNER_ORDER.length + 2);
    expect(titles.slice(-2)).toEqual([sentFirst.message, sentLast.message]);
    expect(titles.slice(1, AAP_BANNER_ORDER.length)).toEqual(
      AAP_BANNER_ORDER.slice(1).map(messageFor),
    );
  });

  it("renders the API's banners in the fixed order, each titled with the API's own message", () => {
    const { discrepancies, tracking, simulated, incomplete, shortWindow } = namedFixtureBanners();
    // Stale arrives after incomplete, and simulated before tracking.
    const { container } = render(
      <GlobalBanners
        banners={[shortWindow, simulated, incomplete, tracking, staleBanner(), discrepancies]}
        overlay={makeOverlay({ open_discrepancies: 3 })}
      />,
    );

    expect(bannerTitles(container)).toEqual([
      '3 open discrepancies to resolve first',
      'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
      'Simulated data: Google (credentials not configured); LinkedIn (credentials not configured); HubSpot (credentials not configured)',
      'Stale data: google.metrics_daily (last success Oct 4, 2026, 5:31 AM UTC)',
      'Incomplete data: google.search_terms (result limit reached at finest partition)',
      'Window of 10 days is below the 14-day trusted minimum',
    ]);
    // The simulated-sources title is the API's message itself, reasons included.
    const simulatedBanner = bannerShowing(simulated.message);
    expect(simulatedBanner.querySelector('.ds-banner__title')?.textContent).toBe(
      simulated.message,
    );
    expect(simulatedBanner.getAttribute('role')).toBeNull();
  });

  it('renders the stale-datasets banner before the incomplete-datasets banner', () => {
    const { incomplete } = namedFixtureBanners();
    const { container } = render(
      <GlobalBanners
        banners={[incomplete, staleBanner()]}
        overlay={makeOverlay({ open_discrepancies: 0 })}
      />,
    );

    expect(bannerTitles(container)).toEqual([
      'Stale data: google.metrics_daily (last success Oct 4, 2026, 5:31 AM UTC)',
      'Incomplete data: google.search_terms (result limit reached at finest partition)',
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
    const shortWindow = bannerShowing('Window of 10 days is below the 14-day trusted minimum');
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
    const { tracking, shortWindow } = namedFixtureBanners();
    // A payload that breaks the contract: the API never sends a null message.
    const missing = { ...tracking, message: null } as unknown as Banner;
    const { container } = render(
      <GlobalBanners
        banners={[{ ...shortWindow, message: '  ' }, missing, makeBanner()]}
        overlay={makeOverlay({ open_discrepancies: 0 })}
      />,
    );

    expect(container.innerHTML).toBe('');
    expect(screen.queryByText('null')).toBeNull();
  });

  it('renders nothing for an empty banner list', () => {
    const { container } = render(<GlobalBanners banners={[]} overlay={makeOverlay()} />);

    expect(container.innerHTML).toBe('');
    expect(container.querySelector('.ds-banner')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders the run notice right after the discrepancy banner, in the same stack as the others', () => {
    const { container } = render(
      <GlobalBanners
        banners={fixtureBanners().reverse()}
        overlay={makeOverlay({ open_discrepancies: 2 })}
        runNotice={runNotice()}
      />,
    );

    // The discrepancy banner sits above everything; the notice comes next, then the rest.
    expect(bannerTitles(container)).toEqual([
      '2 open discrepancies to resolve first',
      RUN_NOTICE_TITLE,
      'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
      'Simulated data: Google (credentials not configured); LinkedIn (credentials not configured); HubSpot (credentials not configured)',
      'Incomplete data: google.search_terms (result limit reached at finest partition)',
      'Window of 10 days is below the 14-day trusted minimum',
    ]);
    // One stack spaces every banner, the notice included, so their gaps are equal.
    const rendered = Array.from(container.querySelectorAll<HTMLElement>('.ds-banner'));
    expect(rendered).toHaveLength(6);
    const [stack] = Array.from(container.children);
    expect(stack?.classList.contains('ds-stack')).toBe(true);
    expect(rendered.every((banner) => banner.parentElement === stack)).toBe(true);
    // The notice is not an alert: the discrepancy banner stays the only one.
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(noticeElement(container).getAttribute('role')).toBeNull();
  });

  it('renders the run notice first when no discrepancy banner is shown', () => {
    const { container } = render(
      <GlobalBanners
        banners={fixtureBanners()}
        overlay={makeOverlay({ open_discrepancies: 0 })}
        runNotice={runNotice()}
      />,
    );

    const titles = bannerTitles(container);
    expect(titles).toHaveLength(5);
    expect(titles[0]).toBe(RUN_NOTICE_TITLE);
    expect(titles[1]).toBe(
      'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
    );
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders the run notice alone when no banner is left, and nothing without one', () => {
    const { container, rerender } = render(
      <GlobalBanners
        banners={[makeBanner()]}
        overlay={makeOverlay({ open_discrepancies: 0 })}
        runNotice={runNotice()}
      />,
    );

    expect(bannerTitles(container)).toEqual([RUN_NOTICE_TITLE]);
    expect(container.querySelector('.ds-stack')).not.toBeNull();

    rerender(
      <GlobalBanners
        banners={[makeBanner()]}
        overlay={makeOverlay({ open_discrepancies: 0 })}
        runNotice={null}
      />,
    );
    expect(container.innerHTML).toBe('');
  });

  it('keeps the same run notice element when the discrepancy banner appears and goes', () => {
    const { container, rerender } = render(
      <GlobalBanners
        banners={fixtureBanners()}
        overlay={makeOverlay({ open_discrepancies: 0 })}
        runNotice={runNotice()}
      />,
    );
    const notice = noticeElement(container);

    rerender(
      <GlobalBanners
        banners={fixtureBanners()}
        overlay={makeOverlay({ open_discrepancies: 1 })}
        runNotice={runNotice()}
      />,
    );
    expect(noticeElement(container)).toBe(notice);
    expect(bannerTitles(container).slice(0, 2)).toEqual([
      '1 open discrepancy to resolve first',
      RUN_NOTICE_TITLE,
    ]);

    rerender(
      <GlobalBanners
        banners={fixtureBanners()}
        overlay={makeOverlay({ open_discrepancies: 0 })}
        runNotice={runNotice()}
      />,
    );
    expect(noticeElement(container)).toBe(notice);
    expect(bannerTitles(container)[0]).toBe(RUN_NOTICE_TITLE);
  });

  it('trims messages, renders an unknown tone as neutral and keeps an unknown code last', () => {
    const consoleError = vi.spyOn(console, 'error');
    const future = makeBanner({
      code: 'future_banner',
      tone: 'emphatic' as BannerTone,
      message: '  A banner code the client does not know yet  ',
      count: null,
    });
    const { tracking } = namedFixtureBanners();
    const trackingAgain = { ...tracking, message: 'A second tracking finding' };
    try {
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
      const unknown = bannerShowing('A banner code the client does not know yet');
      expect(unknown.className).toContain('ds-banner--neutral');
      expect(unknown.className).not.toContain('emphatic');
      // A repeated code gets a key of its own, so React reports no duplicate key.
      expect(consoleError).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
    }
  });
});
