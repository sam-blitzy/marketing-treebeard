/**
 * Contract tests for `TopBar`, the application top bar (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: a plain `div.ds-topbar` (never a second
 * `banner` landmark) with the product name as plain text (never a heading), and the actions row
 * rendered only when `children` renders something: skipped for `undefined`, `null`, booleans,
 * `''` and arrays, at any depth, none of whose items renders; kept for `0`, for an array with
 * one rendering item, and for a non-array iterable, which is passed to React unread.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { TopBar } from './TopBar';

/** `children` values that render nothing, each of which must leave no actions row behind. */
const EMPTY_CHILDREN: readonly [string, ReactNode][] = [
  ['undefined', undefined],
  ['null', null],
  ['false', false],
  ['true', true],
  ['the empty string', ''],
  ['an empty array', []],
  ['an array of signed-out controls', [false, false]],
  ['a nested array of empty values', [[null, ''], [undefined, [true]]]],
];

/**
 * Returns the bar's root element: the parent of its product name.
 *
 * @param productName - the product name text
 * @returns the `div.ds-topbar`
 */
function barOf(productName: string): HTMLElement {
  const root = screen.getByText(productName).parentElement;
  if (root === null) {
    throw new Error(`No top bar shows "${productName}"`);
  }
  return root;
}

describe('TopBar', () => {
  it('renders the product name as plain text in a div, with no heading or landmark', () => {
    render(<TopBar productName="Ads Action Engine" />);

    const product = screen.getByText('Ads Action Engine');
    expect(product.tagName).toBe('SPAN');
    expect(product.className).toBe('ds-topbar__product');

    const bar = barOf('Ads Action Engine');
    expect(bar.tagName).toBe('DIV');
    expect(bar.className).toBe('ds-topbar');
    expect(bar.hasAttribute('role')).toBe(false);
    expect(screen.queryByRole('heading')).toBeNull();
    expect(screen.queryByRole('banner')).toBeNull();
  });

  it('renders its controls in an actions row after the product name', () => {
    const signOut = vi.fn();
    render(
      <TopBar productName="Ads Action Engine">
        <span>admin@example.com</span>
        <button type="button" onClick={signOut}>
          Sign out
        </button>
      </TopBar>,
    );

    const bar = barOf('Ads Action Engine');
    expect(Array.from(bar.children).map((child) => child.className)).toEqual([
      'ds-topbar__product',
      'ds-topbar__actions',
    ]);
    expect(bar.querySelector('.ds-topbar__actions')?.textContent).toBe(
      'admin@example.comSign out',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(signOut).toHaveBeenCalledTimes(1);
  });

  it.each(EMPTY_CHILDREN)('renders no actions row for %s', (_name, children) => {
    render(<TopBar productName="Ads Action Engine">{children}</TopBar>);

    const bar = barOf('Ads Action Engine');
    expect(bar.children).toHaveLength(1);
    expect(bar.querySelector('.ds-topbar__actions')).toBeNull();
  });

  it('renders the actions row for 0, because 0 is content', () => {
    render(<TopBar productName="Ads Action Engine">{0}</TopBar>);

    expect(barOf('Ads Action Engine').querySelector('.ds-topbar__actions')?.textContent).toBe(
      '0',
    );
  });

  it('renders the actions row when one conditional control of several renders', () => {
    render(
      <TopBar productName="Ads Action Engine">
        {false}
        <span>admin@example.com</span>
      </TopBar>,
    );

    expect(barOf('Ads Action Engine').querySelector('.ds-topbar__actions')?.textContent).toBe(
      'admin@example.com',
    );
  });

  it('finds content nested inside arrays', () => {
    render(<TopBar productName="Ads Action Engine">{[[null, ['Signed in']]]}</TopBar>);

    expect(barOf('Ads Action Engine').querySelector('.ds-topbar__actions')?.textContent).toBe(
      'Signed in',
    );
  });

  it('passes a non-array iterable to React unread, so its items still render', () => {
    const controls: Iterable<ReactNode> = new Set<ReactNode>([
      <span key="email">admin@example.com</span>,
    ]);
    render(<TopBar productName="Ads Action Engine">{controls}</TopBar>);

    expect(barOf('Ads Action Engine').querySelector('.ds-topbar__actions')?.textContent).toBe(
      'admin@example.com',
    );
  });
});
