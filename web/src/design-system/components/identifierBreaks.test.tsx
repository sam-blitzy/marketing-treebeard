/**
 * Tests for `withIdentifierBreaks`, the line-break opportunities inside long identifiers of API
 * text (`identifierBreaks.tsx`).
 *
 * They hold it to its TSDoc: a `<wbr>` after each run of underscores inside every identifier of
 * at least sixteen characters, never at either end of one; the text content left exactly as
 * given; a changed string held in one plain `<span>`; and every other input, a short identifier
 * included, returned as given.
 */
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { withIdentifierBreaks } from './identifierBreaks';

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
});
