/**
 * Contract tests for `Spinner`, the loading indicator (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: one polite `role="status"` live region
 * whose only accessible content is the label, visually hidden by default and shown beside the
 * ring with `showLabel`, with the decorative ring hidden from the accessibility tree, and the
 * label following the props.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Spinner } from './Spinner';

describe('Spinner', () => {
  it('is a status live region whose text is the label', () => {
    render(<Spinner label="Loading recommendations" />);

    const status = screen.getByRole('status');
    expect(status.tagName).toBe('SPAN');
    expect(status.className).toBe('ds-spinner');
    expect(status.textContent).toBe('Loading recommendations');
    // role="status" is implicitly polite; no assertive override is added.
    expect(status.hasAttribute('aria-live')).toBe(false);
  });

  it('hides the decorative ring and keeps the label visually hidden, ring first', () => {
    render(<Spinner label="Loading sources" />);

    const parts = Array.from(screen.getByRole('status').children);
    expect(parts.map((part) => part.className)).toEqual([
      'ds-spinner__circle',
      'ds-visually-hidden',
    ]);
    const [ring, label] = parts;
    expect(ring?.getAttribute('aria-hidden')).toBe('true');
    expect(ring?.childNodes).toHaveLength(0);
    expect(label?.hasAttribute('aria-hidden')).toBe(false);
    expect(label?.textContent).toBe('Loading sources');
    expect(screen.getByText('Loading sources')).toBe(label);
  });

  it('follows a changed label', () => {
    const { rerender } = render(<Spinner label="Loading run" />);

    rerender(<Spinner label="Loading decisions" />);

    expect(screen.getAllByRole('status')).toHaveLength(1);
    expect(screen.getByRole('status').textContent).toBe('Loading decisions');
  });

  it('shows the label beside the ring with showLabel, still as the only status text', () => {
    render(<Spinner label="Loading actions" showLabel />);

    const status = screen.getByRole('status');
    expect(status.className).toBe('ds-spinner ds-spinner--labelled');
    expect(status.hasAttribute('aria-live')).toBe(false);
    const parts = Array.from(status.children);
    expect(parts.map((part) => part.className)).toEqual(['ds-spinner__circle', 'ds-spinner__label']);
    const [ring, label] = parts;
    expect(ring?.getAttribute('aria-hidden')).toBe('true');
    expect(ring?.childNodes).toHaveLength(0);
    expect(label?.hasAttribute('aria-hidden')).toBe(false);
    // One text node: the shown label is announced once, never twice.
    expect(label?.childNodes).toHaveLength(1);
    expect(status.textContent).toBe('Loading actions');
    expect(screen.getByText('Loading actions')).toBe(label);
    expect(status.querySelector('.ds-visually-hidden')).toBeNull();
  });

  it('keeps the label visually hidden when showLabel is false, as by default', () => {
    render(<Spinner label="Loading windows" showLabel={false} />);

    const status = screen.getByRole('status');
    expect(status.className).toBe('ds-spinner');
    expect(Array.from(status.children).map((part) => part.className)).toEqual([
      'ds-spinner__circle',
      'ds-visually-hidden',
    ]);
    expect(status.textContent).toBe('Loading windows');
  });

  it('switches between a hidden and a shown label in the same status element', () => {
    const { rerender } = render(<Spinner label="Loading sources" />);
    const status = screen.getByRole('status');

    rerender(<Spinner label="Loading sources" showLabel />);

    expect(screen.getAllByRole('status')).toEqual([status]);
    expect(status.className).toBe('ds-spinner ds-spinner--labelled');
    expect(status.lastElementChild?.className).toBe('ds-spinner__label');
    expect(status.textContent).toBe('Loading sources');

    rerender(<Spinner label="Loading sources" />);

    expect(screen.getAllByRole('status')).toEqual([status]);
    expect(status.className).toBe('ds-spinner');
    expect(status.lastElementChild?.className).toBe('ds-visually-hidden');
    expect(status.textContent).toBe('Loading sources');
  });
});
