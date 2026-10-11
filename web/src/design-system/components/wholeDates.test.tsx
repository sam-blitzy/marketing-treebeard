/**
 * Tests for `withWholeDates`, which keeps each standalone calendar date of a string on one line
 * (`wholeDates.tsx`).
 *
 * They hold it to its TSDoc: each standalone `YYYY-MM-DD` date held in a `ds-whole-date` span;
 * the text content left exactly as given; the dated string held in one plain `<span>`; each run
 * between dates passed through `renderText`; a dateless string returned as `renderText` of it;
 * every other node returned as given; and text that only looks like a date (inside an
 * identifier, an ISO datetime, a longer run of digits) left alone.
 */
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { withIdentifierBreaks } from './identifierBreaks';
import { withWholeDates } from './wholeDates';

/**
 * Renders the helper's output inside a paragraph and returns that paragraph.
 *
 * @param content - the input passed to `withWholeDates`
 * @param renderText - the `renderText` passed to `withWholeDates`, if any
 * @returns the `<p>` holding the output
 */
function rendered(
  content: ReactNode,
  renderText?: (text: string) => ReactNode,
): HTMLParagraphElement {
  const { container } = render(<p>{withWholeDates(content, renderText)}</p>);
  const paragraph = container.querySelector('p');
  if (paragraph === null) {
    throw new Error('the paragraph was not rendered');
  }
  return paragraph;
}

/**
 * Lists the dates the helper held whole, in document order.
 *
 * @param paragraph - the rendered output
 * @returns the text of each `ds-whole-date` span
 */
function wholeDates(paragraph: HTMLElement): (string | null)[] {
  return [...paragraph.querySelectorAll('.ds-whole-date')].map((date) => date.textContent);
}

describe('withWholeDates', () => {
  it('holds a date in a ds-whole-date span inside one plain span, keeping the text', () => {
    const paragraph = rendered('Stale since 2026-10-01');

    expect(paragraph.innerHTML).toBe(
      '<span>Stale since <span class="ds-whole-date">2026-10-01</span></span>',
    );
    expect(paragraph.textContent).toBe('Stale since 2026-10-01');
    // The outer span carries no role or attribute, so the accessible text is the string itself.
    expect(paragraph.firstElementChild?.tagName).toBe('SPAN');
    expect(paragraph.firstElementChild?.attributes).toHaveLength(0);
    // The date span carries its class alone.
    const date = paragraph.querySelector('.ds-whole-date');
    expect(date?.attributes).toHaveLength(1);
  });

  it('holds both dates of a range whole and leaves the spaced en dash between them', () => {
    const title = 'Custom window 2026-08-15 – 2026-10-09';
    const paragraph = rendered(title);

    expect(paragraph.textContent).toBe(title);
    expect(wholeDates(paragraph)).toEqual(['2026-08-15', '2026-10-09']);
    expect(paragraph.innerHTML).toBe(
      '<span>Custom window <span class="ds-whole-date">2026-08-15</span> – ' +
        '<span class="ds-whole-date">2026-10-09</span></span>',
    );
  });

  it('holds a whole string that is a single date, with no empty runs around it', () => {
    const paragraph = rendered('2026-10-09');

    expect(paragraph.innerHTML).toBe('<span><span class="ds-whole-date">2026-10-09</span></span>');
  });

  it('passes each run between dates through renderText, and never a date', () => {
    const runs: string[] = [];
    const paragraph = rendered(
      'From 2026-08-15 to 2026-10-09 inclusive',
      (text) => {
        runs.push(text);
        return <em>{text}</em>;
      },
    );

    expect(runs).toEqual(['From ', ' to ', ' inclusive']);
    expect(paragraph.innerHTML).toBe(
      '<span><em>From </em><span class="ds-whole-date">2026-08-15</span><em> to </em>' +
        '<span class="ds-whole-date">2026-10-09</span><em> inclusive</em></span>',
    );
    expect(paragraph.textContent).toBe('From 2026-08-15 to 2026-10-09 inclusive');
  });

  it('composes with identifier breaks in the text between dates', () => {
    const text = 'Stale since 2026-10-01: HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not read';
    const paragraph = rendered(text, withIdentifierBreaks);

    expect(paragraph.textContent).toBe(text);
    expect(wholeDates(paragraph)).toEqual(['2026-10-01']);
    expect(paragraph.querySelectorAll('wbr')).toHaveLength(4);
    for (const wbr of paragraph.querySelectorAll('wbr')) {
      expect(wbr.previousSibling?.textContent?.endsWith('_')).toBe(true);
    }
  });

  it('returns a dateless string as renderText of it, the string itself by default', () => {
    const text = 'Simulated data: Google (credentials not configured)';
    expect(withWholeDates(text)).toBe(text);
    expect(withWholeDates('')).toBe('');

    const identifier = 'HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured';
    expect(rendered(identifier, withIdentifierBreaks).innerHTML).toBe(
      '<span>HUBSPOT_<wbr>GOOGLE_<wbr>CAMPAIGN_<wbr>PROPERTY not configured</span>',
    );
    const runs: string[] = [];
    withWholeDates(text, (run) => {
      runs.push(run);
      return run;
    });
    expect(runs).toEqual([text]);
  });

  it('keeps a date followed by punctuation or a closing bracket, or after an opening one', () => {
    const text = 'Window (2026-08-15 – 2026-10-09). Last sync 2026-10-11, then 2026-10-12; done';
    const paragraph = rendered(text);

    expect(paragraph.textContent).toBe(text);
    expect(wholeDates(paragraph)).toEqual([
      '2026-08-15',
      '2026-10-09',
      '2026-10-11',
      '2026-10-12',
    ]);
  });

  it('keeps both dates of a range joined by an unspaced en dash', () => {
    const paragraph = rendered('2026-08-15–2026-10-09');

    expect(wholeDates(paragraph)).toEqual(['2026-08-15', '2026-10-09']);
    expect(paragraph.textContent).toBe('2026-08-15–2026-10-09');
  });

  it('leaves an ISO datetime alone', () => {
    expect(withWholeDates('Synced 2026-10-11T05:01:10Z')).toBe('Synced 2026-10-11T05:01:10Z');
    expect(withWholeDates('Synced 2026-10-11T05:01:10+00:00')).toBe(
      'Synced 2026-10-11T05:01:10+00:00',
    );
  });

  it('leaves a date-like run inside an identifier alone, so identifier breaks see it whole', () => {
    expect(withWholeDates('snapshot_2026-10-09 ready')).toBe('snapshot_2026-10-09 ready');
    expect(withWholeDates('2026-10-09_backfill ready')).toBe('2026-10-09_backfill ready');
    expect(withWholeDates('build X2026-10-09 ready')).toBe('build X2026-10-09 ready');
    expect(withWholeDates('größe2026-10-09')).toBe('größe2026-10-09');

    // The identifier keeps its breaks, up to its last underscore, and no date span cuts it.
    const paragraph = rendered('DATASET_SNAPSHOT_FROM_2026-10-09', withIdentifierBreaks);
    expect(paragraph.innerHTML).toBe(
      '<span>DATASET_<wbr>SNAPSHOT_<wbr>FROM_<wbr>2026-10-09</span>',
    );
    expect(wholeDates(paragraph)).toEqual([]);
  });

  it('leaves longer runs of digits and hyphens alone', () => {
    expect(withWholeDates('ref 12026-10-09')).toBe('ref 12026-10-09');
    expect(withWholeDates('ref 2026-10-091')).toBe('ref 2026-10-091');
    expect(withWholeDates('ref 2026-10-09-01')).toBe('ref 2026-10-09-01');
    expect(withWholeDates('ref -2026-10-09')).toBe('ref -2026-10-09');
    expect(withWholeDates('ref 2026-1-09 and 26-10-09')).toBe('ref 2026-1-09 and 26-10-09');
    expect(withWholeDates('ref 2026-10-09abc')).toBe('ref 2026-10-09abc');
  });

  it('returns any node other than a string as given', () => {
    const element = <em>2026-10-09</em>;
    const list = ['2026-10-09'];

    expect(withWholeDates(element)).toBe(element);
    expect(withWholeDates(list)).toBe(list);
    expect(withWholeDates(20261009)).toBe(20261009);
    expect(withWholeDates(null)).toBeNull();
    expect(withWholeDates(undefined)).toBeUndefined();
    expect(withWholeDates(false)).toBe(false);
  });
});
