/**
 * Line-break opportunities inside long identifiers, such as the environment variable names
 * the API quotes in its messages (`HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`).
 *
 * An identifier joined by underscores has no soft wrap opportunity of its own. Where one is wider
 * than the line, the wrappers' `overflow-wrap` breaks it at whatever character reaches the edge
 * ("HUBSPOT_GOOGLE_CAMPAIGN_PROPER" / "TY"). {@link withIdentifierBreaks} adds a `<wbr>` after
 * each run of underscores instead, so the browser breaks such an identifier between its words
 * ("HUBSPOT_GOOGLE_CAMPAIGN_" / "PROPERTY"). `<wbr>` adds no character, so the text content,
 * the accessible name and copied text stay exactly as the API wrote them.
 *
 * `Banner` applies it to a string title or content and `Badge` to a string label, so every
 * banner and chip gets it. The design-system index exports it for API text that feature code
 * places elsewhere, such as the one-sentence reason a section's `EmptyState` gives as its title.
 *
 * GAP: keep applying it in the Blitzy OS banner and chip bindings, unless Blitzy OS breaks such
 * identifiers at underscores itself (`../GAPS.md`).
 *
 * @packageDocumentation
 */
import type { ReactNode } from 'react';

/**
 * The shortest identifier that gets break opportunities, in characters.
 *
 * A shorter one, such as `DATA_MODE`, is narrower than any line a banner or chip lays out down to
 * the 320 px reflow width (WCAG 2.2 SC 1.4.10), so it is left without breaks and always moves to
 * the next line whole. Sixteen upper-case characters of the largest wrapper text, the semibold
 * banner title at `--font-size-md`, take about two thirds of a banner's line at that width.
 */
const MIN_BREAKABLE_LENGTH = 16;

/**
 * An identifier: two or more runs of letters or digits joined by underscores, for example
 * `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY` or `metrics_daily`. Underscores at either end are not
 * part of it, so nothing breaks before a leading or after a trailing underscore.
 */
const IDENTIFIER = /[\p{L}\p{N}]+(?:_+[\p{L}\p{N}]+)+/gu;

/** The positions inside an identifier where a break may fall: after a run of underscores. */
const AFTER_UNDERSCORES = /(?<=_)(?=[\p{L}\p{N}])/u;

/**
 * Adds a line-break opportunity (`<wbr>`) after each run of underscores inside every identifier
 * of at least sixteen characters in a string, so a line wraps between the words of a long
 * identifier rather than at an arbitrary character.
 *
 * - Only a string is changed. Any other node (an element, a number, a list, `null`) is returned
 *   as given, and so is a string with no such identifier, so ordinary text renders exactly as
 *   before.
 * - A changed string is returned as one `<span>` holding its text runs and `<wbr>` elements,
 *   which keeps them a single inline run in every container, a flex container such as the
 *   `Badge` chip included. The span has no role or attributes, so the text content and the
 *   accessible name are the string itself.
 *
 * @param content - the text to render, typically an API message
 * @returns `content` unchanged, or the span with break opportunities
 *
 * @example
 * ```tsx
 * withIdentifierBreaks('HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured');
 * // <span>HUBSPOT_<wbr />GOOGLE_<wbr />CAMPAIGN_<wbr />PROPERTY not configured</span>
 *
 * withIdentifierBreaks('Simulated data: Google (DATA_MODE=simulated)');
 * // 'Simulated data: Google (DATA_MODE=simulated)', unchanged: DATA_MODE is short
 * ```
 */
export function withIdentifierBreaks(content: ReactNode): ReactNode {
  if (typeof content !== 'string') {
    return content;
  }
  const parts: ReactNode[] = [];
  let run = '';
  let consumed = 0;
  for (const match of content.matchAll(IDENTIFIER)) {
    const identifier = match[0];
    if (identifier.length < MIN_BREAKABLE_LENGTH) {
      continue;
    }
    const [first = '', ...rest] = identifier.split(AFTER_UNDERSCORES);
    run += content.slice(consumed, match.index) + first;
    for (const word of rest) {
      parts.push(run, <wbr key={`wbr-${parts.length}`} />);
      run = word;
    }
    consumed = match.index + identifier.length;
  }
  if (parts.length === 0) {
    return content;
  }
  parts.push(run + content.slice(consumed));
  return <span>{parts}</span>;
}
