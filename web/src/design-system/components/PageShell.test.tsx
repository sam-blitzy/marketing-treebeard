/**
 * Contract tests for `PageShell` and its heading slots `PageShell.Title` and `PageShell.Section`
 * (AAP 0.4.2, AAP 0.5.5; `../GAPS.md`, binding procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: a "Skip to main content" link first, which
 * moves focus to `<main>` without rewriting the URL; the top bar inside the one `banner`
 * landmark; the side-navigation column only when `sideNav` renders something (skipped for
 * `undefined`, `null`, booleans and `''`, kept for `0`); `<main id="ds-main" tabindex="-1">`
 * around the content; the page title as the single `<h1>`; and each section a `region` named by
 * its `<h2>` or `<h3>` heading with id `${id}-title`, the actions containers following the same
 * empty-slot rule.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { PageShell } from './PageShell';
import { SideNav } from './SideNav';
import { TopBar } from './TopBar';

/** Slot values React renders as nothing, each of which must leave no container behind. */
const EMPTY_SLOTS: readonly [string, ReactNode][] = [
  ['undefined', undefined],
  ['null', null],
  ['false', false],
  ['true', true],
  ['the empty string', ''],
];

/**
 * Renders the frame the router layout renders: a `TopBar`, the primary `SideNav` and a page.
 *
 * @param children - the page content
 * @returns the render result
 */
function renderShell(children: ReactNode) {
  return render(
    <PageShell
      topBar={<TopBar productName="Ads Action Engine" />}
      sideNav={
        <SideNav
          label="Main"
          items={[
            { key: 'actions', label: 'Actions', href: '/actions', current: true },
            { key: 'decisions', label: 'Decisions', href: '/decisions' },
          ]}
        />
      }
    >
      {children}
    </PageShell>,
  );
}

describe('PageShell', () => {
  it('lays out the skip link, banner, side navigation and main, in that order', () => {
    const { container } = renderShell(<p>Page content</p>);

    const shell = container.firstElementChild;
    expect(shell?.className).toBe('ds-pageshell');
    expect(Array.from(shell?.children ?? []).map((child) => child.className)).toEqual([
      'ds-pageshell__skip ds-visually-hidden ds-visually-hidden--focusable',
      'ds-pageshell__header',
      'ds-pageshell__body',
    ]);

    const banner = screen.getByRole('banner');
    expect(banner.tagName).toBe('HEADER');
    expect(within(banner).getByText('Ads Action Engine')).toBeDefined();

    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(nav.parentElement?.className).toBe('ds-pageshell__sidenav');

    const main = screen.getByRole('main');
    expect(main.id).toBe('ds-main');
    expect(main.getAttribute('tabindex')).toBe('-1');
    expect(main.className).toBe('ds-pageshell__main');
    expect(within(main).getByText('Page content')).toBeDefined();
    expect(nav.parentElement?.nextElementSibling).toBe(main);
  });

  it('renders exactly one banner and one main landmark', () => {
    renderShell(<p>Page content</p>);

    expect(screen.getAllByRole('banner')).toHaveLength(1);
    expect(screen.getAllByRole('main')).toHaveLength(1);
  });

  it('puts the skip link first, targeting main', () => {
    const { container } = renderShell(<button type="button">First control</button>);

    const skip = screen.getByRole('link', { name: 'Skip to main content' });
    expect(skip.getAttribute('href')).toBe('#ds-main');
    expect(container.querySelector('a[href], button')).toBe(skip);
  });

  it('moves focus to main on activation and prevents the URL change', () => {
    renderShell(<p>Page content</p>);
    const hashBefore = window.location.hash;

    const skip = screen.getByRole('link', { name: 'Skip to main content' });
    skip.focus();
    const notCancelled = fireEvent.click(skip);

    expect(notCancelled).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole('main'));
    expect(window.location.hash).toBe(hashBefore);
  });

  it.each(EMPTY_SLOTS)('renders no side-navigation column for %s', (_name, slot) => {
    const { container } = render(
      <PageShell topBar={<TopBar productName="Ads Action Engine" />} sideNav={slot}>
        <p>Page content</p>
      </PageShell>,
    );

    const body = container.querySelector('.ds-pageshell__body');
    expect(body?.children).toHaveLength(1);
    expect(body?.firstElementChild).toBe(screen.getByRole('main'));
    expect(container.querySelector('.ds-pageshell__sidenav')).toBeNull();
  });

  it('renders the side-navigation column for 0, because 0 is content', () => {
    const { container } = render(
      <PageShell topBar={<TopBar productName="Ads Action Engine" />} sideNav={0}>
        <p>Page content</p>
      </PageShell>,
    );

    expect(container.querySelector('.ds-pageshell__sidenav')?.textContent).toBe('0');
  });
});

describe('PageShell.Title', () => {
  it('renders the page title as the single h1, with its actions beside it', () => {
    const sync = vi.fn();
    const { container } = renderShell(
      <PageShell.Title
        actions={
          <button type="button" onClick={sync}>
            Sync now
          </button>
        }
      >
        Actions
      </PageShell.Title>,
    );

    const heading = screen.getByRole('heading', { level: 1, name: 'Actions' });
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(heading.className).toBe('ds-pageshell__title');

    const titlebar = container.querySelector('.ds-pageshell__titlebar');
    expect(heading.parentElement).toBe(titlebar);
    expect(titlebar?.querySelector('.ds-pageshell__actions')?.textContent).toBe('Sync now');

    fireEvent.click(screen.getByRole('button', { name: 'Sync now' }));
    expect(sync).toHaveBeenCalledTimes(1);
  });

  it.each(EMPTY_SLOTS)('renders no actions container for %s', (_name, slot) => {
    const { container } = render(<PageShell.Title actions={slot}>Decisions</PageShell.Title>);

    const titlebar = container.querySelector('.ds-pageshell__titlebar');
    expect(titlebar?.children).toHaveLength(1);
    expect(container.querySelector('.ds-pageshell__actions')).toBeNull();
  });

  it('renders an actions container for 0', () => {
    const { container } = render(<PageShell.Title actions={0}>Sources</PageShell.Title>);

    expect(container.querySelector('.ds-pageshell__actions')?.textContent).toBe('0');
  });
});

describe('PageShell.Section', () => {
  it('is a region with the verbatim id, named by its h2 heading by default', () => {
    render(
      <PageShell.Section id="section-keywords" title="Keywords to adjust">
        <p>Cards</p>
      </PageShell.Section>,
    );

    const region = screen.getByRole('region', { name: 'Keywords to adjust' });
    expect(region.tagName).toBe('SECTION');
    expect(region.id).toBe('section-keywords');
    expect(region.className).toBe('ds-pageshell__section ds-pageshell__section--level-2');
    expect(region.getAttribute('aria-labelledby')).toBe('section-keywords-title');

    const heading = within(region).getByRole('heading', { level: 2, name: 'Keywords to adjust' });
    expect(heading.id).toBe('section-keywords-title');
    expect(heading.className).toBe('ds-pageshell__section-title');
    expect(within(region).getByText('Cards')).toBeDefined();
  });

  it('nests a level-3 group inside a level-2 section without skipping a level', () => {
    render(
      <PageShell.Section id="section-campaigns" title="Campaigns to pause or launch">
        <PageShell.Section
          id="group-campaigns-discrepancies"
          title="Discrepancies"
          headingLevel={3}
        >
          <p>Discrepancy cards</p>
        </PageShell.Section>
      </PageShell.Section>,
    );

    const outer = screen.getByRole('region', { name: 'Campaigns to pause or launch' });
    const group = within(outer).getByRole('region', { name: 'Discrepancies' });
    expect(group.id).toBe('group-campaigns-discrepancies');
    expect(group.className).toBe('ds-pageshell__section ds-pageshell__section--level-3');

    const heading = within(group).getByRole('heading', { level: 3, name: 'Discrepancies' });
    expect(heading.id).toBe('group-campaigns-discrepancies-title');
    expect(screen.getAllByRole('heading').map((h) => h.tagName)).toEqual(['H2', 'H3']);
  });

  it('renders its actions beside the heading, before the content', () => {
    const { container } = render(
      <PageShell.Section
        id="section-budget"
        title="Budget changes"
        actions={<button type="button">Expand all</button>}
      >
        <p>Cards</p>
      </PageShell.Section>,
    );

    const header = container.querySelector('.ds-pageshell__section-header');
    expect(Array.from(header?.children ?? []).map((child) => child.tagName)).toEqual([
      'H2',
      'DIV',
    ]);
    expect(header?.querySelector('.ds-pageshell__actions')?.textContent).toBe('Expand all');
    expect(header?.nextElementSibling?.textContent).toBe('Cards');
  });

  it.each(EMPTY_SLOTS)('renders no actions container for %s', (_name, slot) => {
    const { container } = render(
      <PageShell.Section id="section-copy" title="Copy/creative changes" actions={slot}>
        <p>Cards</p>
      </PageShell.Section>,
    );

    expect(container.querySelector('.ds-pageshell__section-header')?.children).toHaveLength(1);
    expect(container.querySelector('.ds-pageshell__actions')).toBeNull();
  });

  it('renders an actions container for 0', () => {
    const { container } = render(
      <PageShell.Section id="section-white-papers" title="White papers" actions={0}>
        <p>Rows</p>
      </PageShell.Section>,
    );

    expect(container.querySelector('.ds-pageshell__actions')?.textContent).toBe('0');
  });
});
