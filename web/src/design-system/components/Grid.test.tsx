/**
 * Contract tests for `Grid`, the column layout primitive (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: the class contract
 * (`ds-grid ds-grid--{fixed|narrow|wide} ds-grid--gap-{gap}`, default gap 4); a fixed count
 * written to `--ds-grid-columns` as a whole number of at least 1 (fractions floored; zero,
 * negatives and non-finite values give 1 track), and no inline style in auto-fit mode;
 * `role="list"` on `ul` and `ol`; `role="group"` on a `div` only when it has a non-empty
 * `aria-label`; and the `id` forwarded.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SPACE_STEPS } from '../tokens';
import { Grid } from './Grid';

/** Requested column counts and the track count each must normalise to. */
const COLUMN_COUNTS: readonly [number, string][] = [
  [3, '3'],
  [1, '1'],
  [2.7, '2'],
  [0, '1'],
  [0.5, '1'],
  [-3, '1'],
  [Number.NaN, '1'],
  [Number.POSITIVE_INFINITY, '1'],
  [Number.NEGATIVE_INFINITY, '1'],
];

/**
 * Returns the grid's root element: the parent of a cell's text.
 *
 * @param text - the text of one direct child of the grid
 * @returns the grid element
 */
function gridOf(text: string): HTMLElement {
  const root = screen.getByText(text).parentElement;
  if (root === null) {
    throw new Error(`No grid holds "${text}"`);
  }
  return root;
}

describe('Grid', () => {
  it('renders an unlabelled div with no role, the fixed mode and the default gap', () => {
    render(
      <Grid columns={3}>
        <div>Cell</div>
      </Grid>,
    );

    const grid = gridOf('Cell');
    expect(grid.tagName).toBe('DIV');
    expect(grid.className).toBe('ds-grid ds-grid--fixed ds-grid--gap-4');
    expect(grid.hasAttribute('role')).toBe(false);
    expect(grid.hasAttribute('aria-label')).toBe(false);
  });

  it.each(COLUMN_COUNTS)('normalises columns=%s to %s track(s)', (columns, tracks) => {
    render(
      <Grid columns={columns}>
        <div>Cell</div>
      </Grid>,
    );

    const grid = gridOf('Cell');
    expect(grid.classList.contains('ds-grid--fixed')).toBe(true);
    expect(grid.style.getPropertyValue('--ds-grid-columns')).toBe(tracks);
  });

  it.each(['narrow', 'wide'] as const)(
    'uses auto-fit tracks with no inline style for { min: %s }',
    (min) => {
      render(
        <Grid columns={{ min }}>
          <div>Cell</div>
        </Grid>,
      );

      const grid = gridOf('Cell');
      expect(grid.className).toBe(`ds-grid ds-grid--${min} ds-grid--gap-4`);
      expect(grid.hasAttribute('style')).toBe(false);
    },
  );

  it.each(SPACE_STEPS)('carries the gap modifier for step %s', (gap) => {
    render(
      <Grid columns={2} gap={gap}>
        <div>Cell</div>
      </Grid>,
    );

    const gaps = Array.from(gridOf('Cell').classList).filter((name) =>
      name.startsWith('ds-grid--gap-'),
    );
    expect(gaps).toEqual([`ds-grid--gap-${gap}`]);
  });

  it.each(['ul', 'ol'] as const)('is a named list with role="list" as %s', (as) => {
    render(
      <Grid columns={{ min: 'narrow' }} as={as} aria-label="Data sources">
        <li>Google</li>
        <li>LinkedIn</li>
        <li>HubSpot</li>
      </Grid>,
    );

    const list = screen.getByRole('list', { name: 'Data sources' });
    expect(list.tagName).toBe(as.toUpperCase());
    expect(list.getAttribute('role')).toBe('list');
    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Google',
      'LinkedIn',
      'HubSpot',
    ]);
  });

  it('keeps role="list" on an unlabelled list', () => {
    render(
      <Grid columns={2} as="ul">
        <li>Only</li>
      </Grid>,
    );

    expect(screen.getByRole('list').getAttribute('role')).toBe('list');
  });

  it('exposes a labelled div as a named group', () => {
    render(
      <Grid columns={2} aria-label="Evidence">
        <div>Cell</div>
      </Grid>,
    );

    const group = screen.getByRole('group', { name: 'Evidence' });
    expect(group.tagName).toBe('DIV');
    expect(group).toBe(gridOf('Cell'));
  });

  it('gives a div with an empty label no group role', () => {
    render(
      <Grid columns={2} aria-label="">
        <div>Cell</div>
      </Grid>,
    );

    expect(gridOf('Cell').hasAttribute('role')).toBe(false);
    expect(screen.queryByRole('group')).toBeNull();
  });

  it('forwards its id, and renders none when it is omitted', () => {
    render(
      <>
        <Grid columns={2} id="grid-evidence">
          <div>With id</div>
        </Grid>
        <Grid columns={2}>
          <div>Without id</div>
        </Grid>
      </>,
    );

    expect(gridOf('With id').id).toBe('grid-evidence');
    expect(gridOf('Without id').hasAttribute('id')).toBe(false);
  });

  it('renders an empty grid when it has no children', () => {
    const { container } = render(<Grid columns={2} id="grid-empty" />);

    const grid = container.querySelector('#grid-empty');
    expect(grid?.className).toBe('ds-grid ds-grid--fixed ds-grid--gap-4');
    expect(grid?.childNodes).toHaveLength(0);
  });
});
