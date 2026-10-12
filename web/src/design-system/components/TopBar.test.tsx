/**
 * Contract tests for `TopBar`, the application top bar (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: a plain `div.ds-topbar` (never a second
 * `banner` landmark) with the product name as plain text (never a heading), and the actions row
 * rendered only when `children` renders something: skipped for `undefined`, `null`, booleans,
 * `''` and arrays, at any depth, none of whose items renders; kept for `0`, for an array with
 * one rendering item, and for a non-array iterable, which is passed to React unread.
 *
 * They also hold `TopBar.css` to `Button.css`: the product name's line has the Button sizes'
 * min-block-size, so the bar has one height with and without its "Sign out" Button. jsdom lays
 * nothing out, so the declarations are compared as written.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { TopBar } from './TopBar';

/**
 * Returns the value of one declaration in one rule of a stylesheet beside this file, with its
 * whitespace collapsed, so a value the formatter wraps over several lines compares equal to the
 * same value on one line.
 *
 * @param file - the stylesheet's file name, for example `'Button.css'`
 * @param selector - the rule's exact selector, written at the start of a line
 * @param property - the declared property
 * @returns the declared value
 */
function declaredValue(file: string, selector: string, property: string): string {
  const source = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), file), 'utf8');
  const start = source.indexOf(`\n${selector} {\n`);
  if (start === -1) {
    throw new Error(`${file} has no "${selector}" rule`);
  }
  const block = source.slice(start, source.indexOf('\n}', start));
  const match = new RegExp(`\\n\\s*${property}:\\s*([^;]+);`).exec(block);
  if (match?.[1] === undefined) {
    throw new Error(`${file} "${selector}" declares no ${property}`);
  }
  return match[1].replace(/\s+/g, ' ').replace(/\( /g, '(').replace(/ \)/g, ')').trim();
}

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
    const actions = bar.querySelector('.ds-topbar__actions');
    expect(actions?.textContent).toBe('admin@example.comSign out');

    const signOutButton = screen.getByRole('button', { name: 'Sign out' });
    expect(actions?.contains(screen.getByText('admin@example.com'))).toBe(true);
    expect(actions?.contains(signOutButton)).toBe(true);

    // The actions row adds neither a heading nor a second banner landmark: PageShell's <header>
    // is the page's only banner and PageShell.Title its only <h1>.
    expect(screen.queryByRole('heading')).toBeNull();
    expect(screen.queryByRole('banner')).toBeNull();

    fireEvent.click(signOutButton);
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

  it.each(['.ds-button--md', '.ds-button--sm'])(
    'gives the product name the min-block-size of %s, so signing in keeps the bar height',
    (buttonSize) => {
      const productFloor = declaredValue('TopBar.css', '.ds-topbar__product', 'min-block-size');

      expect(productFloor).toBe('calc(var(--space-8) + var(--space-3))');
      expect(declaredValue('Button.css', buttonSize, 'min-block-size')).toBe(productFloor);
    },
  );
});
