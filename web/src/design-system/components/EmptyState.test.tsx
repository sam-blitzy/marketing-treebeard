/**
 * Contract tests for `EmptyState`, the empty-list panel with its reason (AAP 0.4.2, AAP 0.5.5;
 * `../GAPS.md`, binding procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: a `div.ds-emptystate` with the title as a
 * paragraph (never a heading), then the reason and the action, each in its own container only
 * when React would render it (skipped for `undefined`, `null`, booleans and `''`, kept for `0`),
 * with no live region, and the `id` forwarded.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { EmptyState } from './EmptyState';

/** Slot values React renders as nothing, each of which must leave no container behind. */
const EMPTY_SLOTS: readonly [string, ReactNode][] = [
  ['undefined', undefined],
  ['null', null],
  ['false', false],
  ['true', true],
  ['the empty string', ''],
];

/**
 * Returns the empty state's root element: the parent of its title paragraph.
 *
 * @param title - the empty state's title text
 * @returns the `div.ds-emptystate` holding that title
 */
function emptyStateOf(title: string): HTMLElement {
  const root = screen.getByText(title).parentElement;
  if (root === null) {
    throw new Error(`No empty state holds the title "${title}"`);
  }
  return root;
}

describe('EmptyState', () => {
  it('renders the title as a paragraph, never a heading, with no live region', () => {
    render(<EmptyState title="No keywords to adjust" />);

    const title = screen.getByText('No keywords to adjust');
    expect(title.tagName).toBe('P');
    expect(title.className).toBe('ds-emptystate__title');

    const root = emptyStateOf('No keywords to adjust');
    expect(root.tagName).toBe('DIV');
    expect(root.className).toBe('ds-emptystate');
    expect(root.hasAttribute('role')).toBe(false);
    expect(screen.queryByRole('heading')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('renders the reason, then an operable action, after the title', () => {
    const onClick = vi.fn();
    render(
      <EmptyState
        title="No keywords to adjust"
        action={
          <button type="button" onClick={onClick}>
            Open sources
          </button>
        }
      >
        No search term met the K1 minimum of 37 clicks.
      </EmptyState>,
    );

    const root = emptyStateOf('No keywords to adjust');
    expect(Array.from(root.children).map((child) => child.className)).toEqual([
      'ds-emptystate__title',
      'ds-emptystate__reason',
      'ds-emptystate__action',
    ]);
    expect(root.querySelector('.ds-emptystate__reason')?.textContent).toBe(
      'No search term met the K1 minimum of 37 clicks.',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Open sources' }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('renders a section reason and a link to the Sources page without adding a heading', () => {
    render(
      <EmptyState title="No keywords to adjust" action={<a href="/sources">View sources</a>}>
        No search term met the minimum clicks.
      </EmptyState>,
    );

    expect(screen.getByText('No keywords to adjust').className).toBe('ds-emptystate__title');
    expect(screen.getByText('No search term met the minimum clicks.').className).toBe(
      'ds-emptystate__reason',
    );
    expect(screen.queryByRole('heading')).toBeNull();

    const link = screen.getByRole('link', { name: 'View sources' });
    expect(link.getAttribute('href')).toBe('/sources');
    expect(link.parentElement?.className).toBe('ds-emptystate__action');
  });

  it.each(EMPTY_SLOTS)('renders no reason or action container for %s', (_name, slot) => {
    render(
      <EmptyState title="Nothing here" action={slot}>
        {slot}
      </EmptyState>,
    );

    const root = emptyStateOf('Nothing here');
    expect(root.children).toHaveLength(1);
    expect(root.querySelector('.ds-emptystate__reason')).toBeNull();
    expect(root.querySelector('.ds-emptystate__action')).toBeNull();
  });

  it('renders 0 in both slots, because 0 is content', () => {
    render(
      <EmptyState title="Zero" action={0}>
        {0}
      </EmptyState>,
    );

    const root = emptyStateOf('Zero');
    expect(root.querySelector('.ds-emptystate__reason')?.textContent).toBe('0');
    expect(root.querySelector('.ds-emptystate__action')?.textContent).toBe('0');
  });

  it('forwards its id, and renders none when it is omitted', () => {
    render(
      <>
        <EmptyState id="empty-keywords" title="With id" />
        <EmptyState title="Without id" />
      </>,
    );

    expect(emptyStateOf('With id').id).toBe('empty-keywords');
    expect(emptyStateOf('Without id').hasAttribute('id')).toBe(false);
  });
});
