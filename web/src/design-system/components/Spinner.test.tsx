/**
 * Contract tests for `Spinner`, the loading indicator (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: one polite `role="status"` live region
 * whose only accessible content is the visually hidden label, with the decorative ring hidden
 * from the accessibility tree, and the label following the prop.
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
});
