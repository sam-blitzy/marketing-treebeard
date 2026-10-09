/**
 * Contract tests for `Inline`, the horizontal layout primitive (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: the class contract (`ds-inline` and
 * `ds-inline--gap-{gap}` always, default gap 2; `--nowrap` only when `wrap` is `false`;
 * `--align-*`, `--justify-*` and `--break-long-words` only when set), `role="list"` on `ul` and
 * `ol`, `role="group"` on a `div` only when it has a non-empty `aria-label`, and the `id`
 * forwarded.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SPACE_STEPS } from '../tokens';
import { Inline } from './Inline';
import type { InlineProps } from './Inline';

const ALIGNS: readonly NonNullable<InlineProps['align']>[] = [
  'start',
  'center',
  'end',
  'baseline',
  'stretch',
];
const JUSTIFIES: readonly NonNullable<InlineProps['justify']>[] = [
  'start',
  'center',
  'end',
  'between',
];

/**
 * Returns the row's root element: the parent of a child's text.
 *
 * @param text - the text of one direct child of the row
 * @returns the `Inline` element
 */
function rowOf(text: string): HTMLElement {
  const root = screen.getByText(text).parentElement;
  if (root === null) {
    throw new Error(`No row holds "${text}"`);
  }
  return root;
}

describe('Inline', () => {
  it('renders an unlabelled, wrapping div with no role and the default gap', () => {
    render(
      <Inline>
        <span>Item</span>
      </Inline>,
    );

    const row = rowOf('Item');
    expect(row.tagName).toBe('DIV');
    expect(row.className).toBe('ds-inline ds-inline--gap-2');
    expect(row.hasAttribute('role')).toBe(false);
  });

  it.each(SPACE_STEPS)('carries the gap modifier for step %s', (gap) => {
    render(
      <Inline gap={gap}>
        <span>Item</span>
      </Inline>,
    );

    expect(rowOf('Item').className).toBe(`ds-inline ds-inline--gap-${gap}`);
  });

  it('keeps children on one line only when wrap is false', () => {
    render(
      <>
        <Inline wrap={false}>
          <span>One line</span>
        </Inline>
        <Inline wrap>
          <span>Wrapping</span>
        </Inline>
      </>,
    );

    expect(rowOf('One line').classList.contains('ds-inline--nowrap')).toBe(true);
    expect(rowOf('Wrapping').classList.contains('ds-inline--nowrap')).toBe(false);
  });

  it.each(ALIGNS)('carries the align modifier for %s', (align) => {
    render(
      <Inline align={align}>
        <span>Item</span>
      </Inline>,
    );

    expect(rowOf('Item').className).toBe(`ds-inline ds-inline--gap-2 ds-inline--align-${align}`);
  });

  it.each(JUSTIFIES)('carries the justify modifier for %s', (justify) => {
    render(
      <Inline justify={justify}>
        <span>Item</span>
      </Inline>,
    );

    expect(rowOf('Item').className).toBe(
      `ds-inline ds-inline--gap-2 ds-inline--justify-${justify}`,
    );
  });

  it('lets long words break only when breakLongWords is set', () => {
    render(
      <>
        <Inline breakLongWords>
          <span>urn:li:sponsoredCampaign:123456789</span>
        </Inline>
        <Inline breakLongWords={false}>
          <span>Unbroken</span>
        </Inline>
      </>,
    );

    expect(
      rowOf('urn:li:sponsoredCampaign:123456789').classList.contains(
        'ds-inline--break-long-words',
      ),
    ).toBe(true);
    expect(rowOf('Unbroken').classList.contains('ds-inline--break-long-words')).toBe(false);
  });

  it('combines every modifier in the documented order', () => {
    render(
      <Inline gap={4} wrap={false} align="baseline" justify="between" breakLongWords>
        <span>Item</span>
      </Inline>,
    );

    expect(rowOf('Item').className).toBe(
      'ds-inline ds-inline--gap-4 ds-inline--nowrap ds-inline--align-baseline ' +
        'ds-inline--justify-between ds-inline--break-long-words',
    );
  });

  it.each(['ul', 'ol'] as const)('is a named list with role="list" as %s', (as) => {
    render(
      <Inline as={as} gap={1} aria-label="Flags">
        <li>Simulated data</li>
        <li>Stale</li>
      </Inline>,
    );

    const list = screen.getByRole('list', { name: 'Flags' });
    expect(list.tagName).toBe(as.toUpperCase());
    expect(list.getAttribute('role')).toBe('list');
    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Simulated data',
      'Stale',
    ]);
  });

  it('keeps role="list" on an unlabelled list', () => {
    render(
      <Inline as="ol">
        <li>Only</li>
      </Inline>,
    );

    expect(screen.getByRole('list').getAttribute('role')).toBe('list');
  });

  it('exposes a labelled div as a named group', () => {
    render(
      <Inline aria-label="Card actions">
        <button type="button">Record decision</button>
      </Inline>,
    );

    const group = screen.getByRole('group', { name: 'Card actions' });
    expect(group.tagName).toBe('DIV');
    expect(group).toBe(rowOf('Record decision'));
  });

  it('treats an empty label on a div as no label: no group role', () => {
    render(
      <Inline aria-label="">
        <span>Item</span>
      </Inline>,
    );

    expect(rowOf('Item').hasAttribute('role')).toBe(false);
    expect(screen.queryByRole('group')).toBeNull();
  });

  it('forwards its id, and renders none when it is omitted', () => {
    render(
      <>
        <Inline id="row-flags">
          <span>With id</span>
        </Inline>
        <Inline>
          <span>Without id</span>
        </Inline>
      </>,
    );

    expect(rowOf('With id').id).toBe('row-flags');
    expect(rowOf('Without id').hasAttribute('id')).toBe(false);
  });
});
