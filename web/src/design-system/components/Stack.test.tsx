/**
 * Contract tests for `Stack`, the vertical layout primitive (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: the class contract (`ds-stack` and
 * `ds-stack--gap-{gap}` always, default gap 4; `--align-*` and `--break-long-words` only when
 * set), `role="list"` on `ul` and `ol`, a `section` that keeps its native role and becomes a
 * `region` landmark only when named, `role="group"` on a `div` only when it has a non-empty
 * `aria-label`, and the `id` forwarded.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SPACE_STEPS } from '../tokens';
import { Stack } from './Stack';
import type { StackProps } from './Stack';

const ALIGNS: readonly NonNullable<StackProps['align']>[] = ['start', 'center', 'end', 'stretch'];

/**
 * Returns the stack's root element: the parent of a child's text.
 *
 * @param text - the text of one direct child of the stack
 * @returns the `Stack` element
 */
function stackOf(text: string): HTMLElement {
  const root = screen.getByText(text).parentElement;
  if (root === null) {
    throw new Error(`No stack holds "${text}"`);
  }
  return root;
}

describe('Stack', () => {
  it('renders an unlabelled div with no role and the default gap', () => {
    render(
      <Stack>
        <div>Item</div>
      </Stack>,
    );

    const stack = stackOf('Item');
    expect(stack.tagName).toBe('DIV');
    expect(stack.className).toBe('ds-stack ds-stack--gap-4');
    expect(stack.hasAttribute('role')).toBe(false);
  });

  it.each(SPACE_STEPS)('carries the gap modifier for step %s', (gap) => {
    render(
      <Stack gap={gap}>
        <div>Item</div>
      </Stack>,
    );

    expect(stackOf('Item').className).toBe(`ds-stack ds-stack--gap-${gap}`);
  });

  it.each(ALIGNS)('carries the align modifier for %s', (align) => {
    render(
      <Stack align={align}>
        <div>Item</div>
      </Stack>,
    );

    expect(stackOf('Item').className).toBe(`ds-stack ds-stack--gap-4 ds-stack--align-${align}`);
  });

  it('lets long words break only when breakLongWords is set, after the layout classes', () => {
    render(
      <>
        <Stack gap={6} align="start" breakLongWords>
          <div>Breaking</div>
        </Stack>
        <Stack breakLongWords={false}>
          <div>Unbroken</div>
        </Stack>
      </>,
    );

    expect(stackOf('Breaking').className).toBe(
      'ds-stack ds-stack--gap-6 ds-stack--align-start ds-stack--break-long-words',
    );
    expect(stackOf('Unbroken').className).toBe('ds-stack ds-stack--gap-4');
  });

  it.each(['ul', 'ol'] as const)('is a named list with role="list" as %s', (as) => {
    render(
      <Stack as={as} gap={3} aria-label="Keyword actions">
        <li>Block</li>
        <li>Expand</li>
      </Stack>,
    );

    const list = screen.getByRole('list', { name: 'Keyword actions' });
    expect(list.tagName).toBe(as.toUpperCase());
    expect(list.getAttribute('role')).toBe('list');
    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Block',
      'Expand',
    ]);
  });

  it('keeps role="list" on an unlabelled list', () => {
    render(
      <Stack as="ul">
        <li>Only</li>
      </Stack>,
    );

    expect(screen.getByRole('list').getAttribute('role')).toBe('list');
  });

  it('renders a named section as a region landmark, with no explicit role', () => {
    render(
      <Stack as="section" id="budget" aria-label="Budget changes">
        <div>Card</div>
      </Stack>,
    );

    const region = screen.getByRole('region', { name: 'Budget changes' });
    expect(region.tagName).toBe('SECTION');
    expect(region.id).toBe('budget');
    expect(region.hasAttribute('role')).toBe(false);
  });

  it('gives an unnamed section no explicit role', () => {
    render(
      <Stack as="section">
        <div>Card</div>
      </Stack>,
    );

    const section = stackOf('Card');
    expect(section.tagName).toBe('SECTION');
    expect(section.hasAttribute('role')).toBe(false);
    expect(section.hasAttribute('aria-label')).toBe(false);
  });

  it('exposes a labelled div as a named group', () => {
    render(
      <Stack aria-label="Evidence">
        <div>Row</div>
      </Stack>,
    );

    const group = screen.getByRole('group', { name: 'Evidence' });
    expect(group.tagName).toBe('DIV');
    expect(group).toBe(stackOf('Row'));
  });

  it('treats an empty label on a div as no label: no group role', () => {
    render(
      <Stack aria-label="">
        <div>Row</div>
      </Stack>,
    );

    expect(stackOf('Row').hasAttribute('role')).toBe(false);
    expect(screen.queryByRole('group')).toBeNull();
  });

  it('forwards its id, and renders none when it is omitted', () => {
    render(
      <>
        <Stack id="stack-main">
          <div>With id</div>
        </Stack>
        <Stack>
          <div>Without id</div>
        </Stack>
      </>,
    );

    expect(stackOf('With id').id).toBe('stack-main');
    expect(stackOf('Without id').hasAttribute('id')).toBe(false);
  });
});
