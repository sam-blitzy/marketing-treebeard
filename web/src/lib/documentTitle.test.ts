/**
 * Unit tests for src/lib/documentTitle.ts, the document title of every screen (WCAG 2.2 SC 2.4.2
 * Page Titled).
 *
 * - `useDocumentTitle` sets the title to the page's title, a middle dot and the product name,
 *   writes it again when the page's title changes, ignores the title's surrounding whitespace,
 *   leaves the product name alone for a blank title, and keeps the title when its caller
 *   unmounts.
 *
 * The hook runs through Testing Library's `renderHook`, in jsdom. The title is reset before each
 * test, so no test reads a title an earlier one wrote.
 */
import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { useDocumentTitle } from './documentTitle';

beforeEach(() => {
  document.title = '';
});

describe('useDocumentTitle', () => {
  it('names the page, then the product, separated by a middle dot', () => {
    renderHook(() => {
      useDocumentTitle('Decisions');
    });

    expect(document.title).toBe('Decisions · Ads Action Engine');
    expect(document.title).toContain(' \u00B7 ');
  });

  it('writes the title again when the page title changes', () => {
    const { rerender } = renderHook(
      ({ title }: { title: string }) => {
        useDocumentTitle(title);
      },
      { initialProps: { title: 'Sign in' } },
    );
    expect(document.title).toBe('Sign in · Ads Action Engine');

    rerender({ title: 'Page not found' });

    expect(document.title).toBe('Page not found · Ads Action Engine');
  });

  it('uses the page title without its surrounding whitespace', () => {
    renderHook(() => {
      useDocumentTitle('  Sources\n');
    });

    expect(document.title).toBe('Sources · Ads Action Engine');
  });

  it.each([
    ['an empty title', ''],
    ['a whitespace-only title', ' \t '],
  ])('falls back to the product name alone for %s', (_case, title) => {
    renderHook(() => {
      useDocumentTitle(title);
    });

    expect(document.title).toBe('Ads Action Engine');
  });

  it('keeps the title when its caller unmounts, until the next screen sets its own', () => {
    const { unmount } = renderHook(() => {
      useDocumentTitle('Settings');
    });

    unmount();

    expect(document.title).toBe('Settings · Ads Action Engine');
  });
});
