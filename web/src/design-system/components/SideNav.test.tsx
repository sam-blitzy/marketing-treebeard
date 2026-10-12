/**
 * Contract tests for `SideNav`, the section and primary navigation (AAP 0.4.2; `../GAPS.md`,
 * binding procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: a `<nav>` landmark named by `label` holding
 * one list item per destination, in order; each link named by its label and count ("Keywords to
 * adjust 4"), the count digit-grouped as `formatCount` groups it ("Keywords to adjust 20,262"),
 * with `0` shown and a missing or non-finite count omitted; the current page marked
 * by `aria-current="page"` and the `--current` modifier, and no `aria-current` anywhere else;
 * only same-origin route paths and in-page anchors rendered as destinations, every other `href`
 * failing closed to an inert `<a>` without `href`; and `renderLink` called once per safe item
 * with the validated props, never for an unsafe one.
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SideNav } from './SideNav';
import type { SideNavItem, SideNavLinkProps } from './SideNav';

/** Destinations SideNav must render as links. */
const SAFE_HREFS: readonly string[] = [
  '/decisions',
  '/actions?run=3',
  '/actions#section-budget',
  '/',
  '#section-keywords',
];

/** Destinations SideNav must never render, each with the reason it is unsafe. */
const UNSAFE_HREFS: readonly [string, string][] = [
  ['a javascript: URL', 'javascript:alert(1)'],
  ['a data: URL', 'data:text/html,<p>x</p>'],
  ['an absolute https: URL', 'https://evil.example/'],
  ['a mailto: URL', 'mailto:someone@example.com'],
  ['a protocol-relative URL', '//evil.example'],
  ['a slash then a backslash', '/\\evil.example'],
  ['a relative path', 'decisions'],
  ['the empty string', ''],
  ['a tab that browsers strip', '/\t/evil.example'],
  ['a newline', '/actions\n'],
  ['a space', '/actions run'],
  ['a NUL control character', '/actions\u0000'],
  ['a DELETE control character', '/actions\u007f'],
  ['an inner backslash', '/actions\\x'],
  ['a non-ASCII space', '#section\u00a0keywords'],
];

/** The /actions section navigation from the component's own example. */
const SECTIONS: readonly SideNavItem[] = [
  {
    key: 'keywords',
    label: 'Keywords to adjust',
    href: '#section-keywords',
    count: 4,
    current: true,
  },
  { key: 'campaigns', label: 'Campaigns to pause or launch', href: '#section-campaigns', count: 0 },
  { key: 'budget', label: 'Budget changes', href: '#section-budget' },
];

describe('SideNav', () => {
  it('is a navigation landmark named by its label, with one list item per destination', () => {
    render(<SideNav label="Sections" items={SECTIONS} />);

    const nav = screen.getByRole('navigation', { name: 'Sections' });
    expect(nav.className).toBe('ds-sidenav');

    const list = within(nav).getByRole('list');
    expect(list.className).toBe('ds-sidenav__list');
    const items = within(list).getAllByRole('listitem');
    expect(items.map((item) => item.className)).toEqual([
      'ds-sidenav__item',
      'ds-sidenav__item',
      'ds-sidenav__item',
    ]);
    expect(
      within(nav)
        .getAllByRole('link')
        .map((link) => link.getAttribute('href')),
    ).toEqual(['#section-keywords', '#section-campaigns', '#section-budget']);
  });

  it('names each link by its label and count, showing 0 and omitting a missing count', () => {
    render(<SideNav label="Sections" items={SECTIONS} />);

    expect(screen.getAllByRole('link').map((link) => link.textContent)).toEqual([
      'Keywords to adjust 4',
      'Campaigns to pause or launch 0',
      'Budget changes',
    ]);
    expect(screen.getByRole('link', { name: 'Keywords to adjust 4' })).toBeDefined();
    expect(screen.getByRole('link', { name: 'Campaigns to pause or launch 0' })).toBeDefined();

    const budget = screen.getByRole('link', { name: 'Budget changes' });
    expect(budget.querySelector('.ds-sidenav__label')?.textContent).toBe('Budget changes');
    expect(budget.querySelector('.ds-sidenav__count')).toBeNull();

    const keywords = screen.getByRole('link', { name: 'Keywords to adjust 4' });
    expect(keywords.querySelector('.ds-sidenav__count')?.textContent).toBe('4');
  });

  it.each([
    ['NaN', Number.NaN],
    ['Infinity', Number.POSITIVE_INFINITY],
    ['-Infinity', Number.NEGATIVE_INFINITY],
  ])('renders no count for %s', (_name, count) => {
    render(
      <SideNav label="Sections" items={[{ key: 'copy', label: 'Copy', href: '#copy', count }]} />,
    );

    const link = screen.getByRole('link', { name: 'Copy' });
    expect(link.textContent).toBe('Copy');
    expect(link.querySelector('.ds-sidenav__count')).toBeNull();
  });

  it.each([
    [20262, '20,262'],
    [18002, '18,002'],
    [1000, '1,000'],
    [1234567, '1,234,567'],
    [999, '999'],
    [0, '0'],
  ])('groups the digits of the count %d as "%s", in its text and its name', (count, text) => {
    render(
      <SideNav
        label="Sections"
        items={[{ key: 'keywords', label: 'Keywords to adjust', href: '#section-keywords', count }]}
      />,
    );

    const link = screen.getByRole('link', { name: `Keywords to adjust ${text}` });
    expect(link.textContent).toBe(`Keywords to adjust ${text}`);
    expect(link.querySelector('.ds-sidenav__count')?.textContent).toBe(text);
  });

  it('hands renderLink the grouped count, as the default link renders it', () => {
    render(
      <SideNav
        label="Main"
        items={[{ key: 'actions', label: 'Actions', href: '/actions', count: 20262 }]}
        renderLink={(_item, linkProps) => (
          <a href={linkProps.href} className={linkProps.className}>
            {linkProps.children}
          </a>
        )}
      />,
    );

    const link = screen.getByRole('link', { name: 'Actions 20,262' });
    expect(link.querySelector('.ds-sidenav__count')?.textContent).toBe('20,262');
  });

  it('marks only the current item, by aria-current="page" and the current modifier', () => {
    render(<SideNav label="Sections" items={SECTIONS} />);

    const current = screen.getByRole('link', { current: 'page' });
    expect(current.textContent).toBe('Keywords to adjust 4');
    expect(current.className).toBe('ds-sidenav__link ds-sidenav__link--current');

    const others = screen.getAllByRole('link', { current: false });
    expect(others.map((link) => link.textContent)).toEqual([
      'Campaigns to pause or launch 0',
      'Budget changes',
    ]);
    for (const link of others) {
      expect(link.hasAttribute('aria-current')).toBe(false);
      expect(link.className).toBe('ds-sidenav__link');
    }
  });

  it('treats current: false like an omitted current', () => {
    render(
      <SideNav
        label="Main"
        items={[{ key: 'sources', label: 'Sources', href: '/sources', current: false }]}
      />,
    );

    const link = screen.getByRole('link', { name: 'Sources' });
    expect(link.hasAttribute('aria-current')).toBe(false);
    expect(link.className).toBe('ds-sidenav__link');
  });

  it.each(SAFE_HREFS)('renders %s as a link destination', (href) => {
    render(<SideNav label="Main" items={[{ key: 'only', label: 'Destination', href }]} />);

    expect(screen.getByRole('link', { name: 'Destination' }).getAttribute('href')).toBe(href);
  });

  it.each(UNSAFE_HREFS)('fails closed for %s: an inert placeholder without href', (_name, href) => {
    const renderLink = vi.fn((_item: SideNavItem, linkProps: SideNavLinkProps) => (
      <a {...linkProps} />
    ));
    render(
      <SideNav
        label="Main"
        items={[{ key: 'unsafe', label: 'Unsafe', href, count: 2, current: true }]}
        renderLink={renderLink}
      />,
    );

    expect(renderLink).not.toHaveBeenCalled();
    expect(screen.queryByRole('link')).toBeNull();

    const placeholder = screen.getByText('Unsafe').parentElement;
    expect(placeholder?.tagName).toBe('A');
    expect(placeholder?.hasAttribute('href')).toBe(false);
    expect(placeholder?.className).toBe('ds-sidenav__link ds-sidenav__link--current');
    expect(placeholder?.getAttribute('aria-current')).toBe('page');
    expect(placeholder?.textContent).toBe('Unsafe 2');
  });

  it('renders a non-current unsafe item as a placeholder without aria-current', () => {
    render(
      <SideNav
        label="Main"
        items={[{ key: 'unsafe', label: 'Unsafe', href: 'javascript:void(0)' }]}
      />,
    );

    const placeholder = screen.getByText('Unsafe').parentElement;
    expect(placeholder?.hasAttribute('href')).toBe(false);
    expect(placeholder?.hasAttribute('aria-current')).toBe(false);
    expect(placeholder?.className).toBe('ds-sidenav__link');
  });

  it('keeps safe items as links beside an unsafe one', () => {
    render(
      <SideNav
        label="Main"
        items={[
          { key: 'actions', label: 'Actions', href: '/actions' },
          { key: 'bad', label: 'Bad', href: 'https://evil.example/' },
          { key: 'sources', label: 'Sources', href: '/sources' },
        ]}
      />,
    );

    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect(screen.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      '/actions',
      '/sources',
    ]);
  });

  it('calls renderLink once per safe item with the validated link props', () => {
    const calls: [SideNavItem, SideNavLinkProps][] = [];
    const renderLink = (item: SideNavItem, linkProps: SideNavLinkProps) => {
      calls.push([item, linkProps]);
      return <a {...linkProps} data-router-link={item.key} />;
    };
    const items: readonly SideNavItem[] = [
      { key: 'actions', label: 'Actions', href: '/actions', current: true },
      { key: 'decisions', label: 'Decisions', href: '/decisions', count: 2 },
    ];
    render(<SideNav label="Main" items={items} renderLink={renderLink} />);

    expect(calls.map(([item]) => item)).toEqual(items);
    expect(calls.map(([, linkProps]) => linkProps.href)).toEqual(['/actions', '/decisions']);
    expect(calls.map(([, linkProps]) => linkProps.className)).toEqual([
      'ds-sidenav__link ds-sidenav__link--current',
      'ds-sidenav__link',
    ]);
    expect(calls.map(([, linkProps]) => 'aria-current' in linkProps)).toEqual([true, false]);
    expect(calls.map(([, linkProps]) => linkProps['aria-current'])).toEqual(['page', undefined]);

    const actions = screen.getByRole('link', { name: 'Actions', current: 'page' });
    expect(actions.getAttribute('data-router-link')).toBe('actions');
    expect(actions.getAttribute('href')).toBe('/actions');

    const decisions = screen.getByRole('link', { name: 'Decisions 2' });
    expect(decisions.getAttribute('data-router-link')).toBe('decisions');
    expect(decisions.hasAttribute('aria-current')).toBe(false);
  });

  it('renders the named landmark with an empty list when there are no items', () => {
    render(<SideNav label="Sections" items={[]} />);

    const nav = screen.getByRole('navigation', { name: 'Sections' });
    expect(within(nav).getByRole('list').children).toHaveLength(0);
    expect(within(nav).queryByRole('link')).toBeNull();
  });
});
