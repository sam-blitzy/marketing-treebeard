/**
 * Tests for `withIdentifierBreaks` and `withTokenBreaks`, the line-break opportunities inside
 * long identifiers and tokens of API text (`identifierBreaks.tsx`).
 *
 * They hold each to its TSDoc: a `<wbr>` after each run of underscores inside every identifier,
 * or after each run of separators (`.`, `_`, `:`, `=`, `/`, `\`, `@`, `&`, `?`, `#`, `+`, `|`)
 * inside every token, of at least sixteen characters, never at either end of one and never
 * inside a word; the text content left exactly as given; a changed string held in one plain
 * `<span>`; and every other input, a short identifier or token, prose and hyphenated text
 * included, returned as given. `withIdentifierBreaks`, which Banner and Badge apply, breaks at
 * underscores only.
 */
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { withIdentifierBreaks, withTokenBreaks } from './identifierBreaks';

/** The message the API writes for the `no_crm_link` finding when the Google link is unset. */
const NO_CRM_LINK =
  'No CRM link for Google campaigns: HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and ' +
  'HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured.';

/**
 * Renders the helper's output inside a paragraph and returns that paragraph.
 *
 * @param content - the input passed to `withIdentifierBreaks`
 * @returns the `<p>` holding the output
 */
function rendered(content: ReactNode): HTMLParagraphElement {
  const { container } = render(<p>{withIdentifierBreaks(content)}</p>);
  const paragraph = container.querySelector('p');
  if (paragraph === null) {
    throw new Error('the paragraph was not rendered');
  }
  return paragraph;
}

describe('withIdentifierBreaks', () => {
  it('breaks a long identifier after each underscore, keeping its text', () => {
    const paragraph = rendered('HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured');

    expect(paragraph.innerHTML).toBe(
      '<span>HUBSPOT_<wbr>GOOGLE_<wbr>CAMPAIGN_<wbr>PROPERTY not configured</span>',
    );
    expect(paragraph.textContent).toBe('HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured');
  });

  it('breaks every long identifier of an API message and leaves the words around them', () => {
    const paragraph = rendered(NO_CRM_LINK);

    expect(paragraph.textContent).toBe(NO_CRM_LINK);
    expect(paragraph.innerHTML).toBe(
      '<span>No CRM link for Google campaigns: HUBSPOT_<wbr>GOOGLE_<wbr>CAMPAIGN_<wbr>PROPERTY ' +
        'and HUBSPOT_<wbr>GOOGLE_<wbr>TOUCH_<wbr>DATE_<wbr>PROPERTY not configured.</span>',
    );
    // Every break follows an underscore and precedes a letter or digit.
    const span = paragraph.firstElementChild;
    expect(span?.tagName).toBe('SPAN');
    expect(span?.attributes).toHaveLength(0);
    for (const wbr of paragraph.querySelectorAll('wbr')) {
      expect(wbr.previousSibling?.textContent?.endsWith('_')).toBe(true);
      expect(wbr.nextSibling?.textContent).toMatch(/^[A-Z]/u);
    }
  });

  it('leaves an identifier shorter than sixteen characters whole', () => {
    const text = 'Simulated data: Google (DATA_MODE=simulated); LinkedIn (DATA_MODE=simulated)';
    expect(withIdentifierBreaks(text)).toBe(text);

    // Fifteen characters stay whole; sixteen get their break.
    expect(withIdentifierBreaks('ABCDEFG_HIJKLMN')).toBe('ABCDEFG_HIJKLMN');
    expect(rendered('ABCDEFG_HIJKLMNO').innerHTML).toBe('<span>ABCDEFG_<wbr>HIJKLMNO</span>');
  });

  it('breaks after a run of underscores, never before a leading or after a trailing one', () => {
    const paragraph = rendered('__linkedin_lead__form_responses_ synced');

    expect(paragraph.textContent).toBe('__linkedin_lead__form_responses_ synced');
    expect(paragraph.innerHTML).toBe(
      '<span>__linkedin_<wbr>lead__<wbr>form_<wbr>responses_ synced</span>',
    );
  });

  it('treats letters and digits of any script as identifier characters', () => {
    const paragraph = rendered('größe_der_kampagne_2026 fehlt');

    expect(paragraph.textContent).toBe('größe_der_kampagne_2026 fehlt');
    expect(paragraph.querySelectorAll('wbr')).toHaveLength(3);
  });

  it('returns text without an identifier, and the empty string, unchanged', () => {
    expect(withIdentifierBreaks('Simulated data (Google)')).toBe('Simulated data (Google)');
    expect(withIdentifierBreaks('snake_case')).toBe('snake_case');
    expect(withIdentifierBreaks('')).toBe('');
  });

  it('returns any node other than a string as given', () => {
    const element = <em>HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY</em>;
    const list = ['HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY'];

    expect(withIdentifierBreaks(element)).toBe(element);
    expect(withIdentifierBreaks(list)).toBe(list);
    expect(withIdentifierBreaks(0)).toBe(0);
    expect(withIdentifierBreaks(null)).toBeNull();
    expect(withIdentifierBreaks(undefined)).toBeUndefined();
    expect(withIdentifierBreaks(false)).toBe(false);
  });

  it('leaves the other separators of a token alone', () => {
    expect(withIdentifierBreaks('Simulated (hubspot.deal_search)')).toBe(
      'Simulated (hubspot.deal_search)',
    );
    expect(withIdentifierBreaks('urn:li:leadGenForm:7001')).toBe('urn:li:leadGenForm:7001');
  });
});

/**
 * Renders `withTokenBreaks`' output inside a paragraph and returns that paragraph.
 *
 * @param content - the input passed to `withTokenBreaks`
 * @returns the `<p>` holding the output
 */
function renderedTokens(content: ReactNode): HTMLParagraphElement {
  const { container } = render(<p>{withTokenBreaks(content)}</p>);
  const paragraph = container.querySelector('p');
  if (paragraph === null) {
    throw new Error('the paragraph was not rendered');
  }
  return paragraph;
}

describe('withTokenBreaks', () => {
  it.each([
    ['hubspot.deal_search', '<span>hubspot.<wbr>deal_<wbr>search</span>'],
    ['urn:li:leadGenForm:7001', '<span>urn:<wbr>li:<wbr>leadGenForm:<wbr>7001</span>'],
    ['DATA_MODE=simulated', '<span>DATA_<wbr>MODE=<wbr>simulated</span>'],
    ['config/thresholds.yaml', '<span>config/<wbr>thresholds.<wbr>yaml</span>'],
    ['admin@example.com', '<span>admin@<wbr>example.<wbr>com</span>'],
    [
      'https://app.example.com/a?b=1&c=2',
      '<span>https://<wbr>app.<wbr>example.<wbr>com/<wbr>a?<wbr>b=<wbr>1&amp;<wbr>c=<wbr>2</span>',
    ],
    ['C:\\exports\\leads.csv', '<span>C:\\<wbr>exports\\<wbr>leads.<wbr>csv</span>'],
    [
      'HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY',
      '<span>HUBSPOT_<wbr>GOOGLE_<wbr>CAMPAIGN_<wbr>PROPERTY</span>',
    ],
  ])('breaks %s after each run of separators, keeping its text', (token, markup) => {
    const paragraph = renderedTokens(token);

    expect(paragraph.innerHTML).toBe(markup);
    expect(paragraph.textContent).toBe(token);
  });

  it('breaks every long token of a message and leaves the words around them whole', () => {
    const message = 'Simulated (DATA_MODE=simulated) for hubspot.deal_search since 9:30.';
    const paragraph = renderedTokens(message);

    expect(paragraph.textContent).toBe(message);
    expect(paragraph.innerHTML).toBe(
      '<span>Simulated (DATA_<wbr>MODE=<wbr>simulated) for hubspot.<wbr>deal_<wbr>search ' +
        'since 9:30.</span>',
    );
    // The single span has no attributes, and every break follows a separator and precedes a
    // letter or digit: none falls inside a word.
    const span = paragraph.firstElementChild;
    expect(span?.tagName).toBe('SPAN');
    expect(span?.attributes).toHaveLength(0);
    for (const wbr of paragraph.querySelectorAll('wbr')) {
      expect(wbr.previousSibling?.textContent).toMatch(/[._:=/\\@&?#+|]$/u);
      expect(wbr.nextSibling?.textContent).toMatch(/^[\p{L}\p{N}]/u);
    }
  });

  it('leaves a token shorter than sixteen characters, prose and hyphens whole', () => {
    // Fifteen characters stay whole; sixteen get their breaks.
    expect(withTokenBreaks('google.keywords')).toBe('google.keywords');
    expect(renderedTokens('hubspot.contacts').innerHTML).toBe(
      '<span>hubspot.<wbr>contacts</span>',
    );
    for (const text of [
      'Platform state at first sync',
      'EUR 1,234,567.89 at 9:30, e.g. today',
      'b2b-pipeline-report-2026',
      'Covered 2026-04-09 – 2026-10-09',
      '',
    ]) {
      expect(withTokenBreaks(text)).toBe(text);
    }
  });

  it('breaks after a run of separators, never before a leading or after a trailing one', () => {
    const paragraph = renderedTokens('./linkedin..lead_form_responses. synced');

    expect(paragraph.textContent).toBe('./linkedin..lead_form_responses. synced');
    expect(paragraph.innerHTML).toBe(
      '<span>./linkedin..<wbr>lead_<wbr>form_<wbr>responses. synced</span>',
    );
  });

  it('treats letters and digits of any script as token characters', () => {
    const paragraph = renderedTokens('größe.der_kampagne:2026 fehlt');

    expect(paragraph.textContent).toBe('größe.der_kampagne:2026 fehlt');
    expect(paragraph.querySelectorAll('wbr')).toHaveLength(3);
  });

  it('returns any node other than a string as given', () => {
    const element = <code>hubspot.deal_search</code>;
    const list = ['hubspot.deal_search'];

    expect(withTokenBreaks(element)).toBe(element);
    expect(withTokenBreaks(list)).toBe(list);
    expect(withTokenBreaks(1234567890123456)).toBe(1234567890123456);
    expect(withTokenBreaks(null)).toBeNull();
    expect(withTokenBreaks(undefined)).toBeUndefined();
    expect(withTokenBreaks(true)).toBe(true);
  });
});
