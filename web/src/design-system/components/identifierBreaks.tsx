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
 * {@link withTokenBreaks} does the same for every separator of a machine token, not only
 * underscores: the dots of a dataset name (`hubspot.deal_search`), the colons of a URN
 * (`urn:li:leadGenForm:7001`), the `=` of a setting (`DATA_MODE=simulated`), the slashes of a
 * path. `DataTable` applies it to every string header, cell and note, because a table cell keeps
 * its words whole: its column is as wide as its longest unbreakable run and the table scrolls in
 * its region past that, so a token breaks only at its separators. The index exports it for a
 * token that a table cell renders inside an element, such as `<code>`.
 *
 * GAP: keep applying them in the Blitzy OS banner, chip and table bindings, unless Blitzy OS breaks
 * such identifiers and tokens at their separators itself (`../GAPS.md`).
 *
 * @packageDocumentation
 */
import type { ReactNode } from 'react';

/**
 * The shortest identifier or token that gets break opportunities, in characters.
 *
 * A shorter one, such as `DATA_MODE`, is narrower than any line a banner or chip lays out down to
 * the 320 px reflow width (WCAG 2.2 SC 1.4.10), so it is left without breaks and always moves to
 * the next line whole. Sixteen upper-case characters of the largest wrapper text, the semibold
 * banner title at `--font-size-md`, take about two thirds of a banner's line at that width. In a
 * table cell the same bound keeps short figures and words with a separator, such as `9:30`,
 * `240.00`, `e.g.` or `Etc/UTC`, whole, while a dataset name, URN or setting breaks into its parts.
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
 * A machine token: two or more runs of letters or digits joined by runs of separators, for
 * example `hubspot.deal_search`, `urn:li:leadGenForm:7001`, `DATA_MODE=simulated`,
 * `admin@example.com` or `config/thresholds.yaml`. The separators are `.`, `_`, `:`, `=`, `/`,
 * `\`, `@`, `&`, `?`, `#`, `+` and `|`. Separators at either end are not part of it, so nothing
 * breaks before a leading or after a trailing one, such as the full stop ending a sentence. A
 * hyphen is not a separator: a browser already offers a break after a hyphen between words, and
 * dates join their parts with hyphens.
 */
const TOKEN = /[\p{L}\p{N}]+(?:[._:=/\\@&?#+|]+[\p{L}\p{N}]+)+/gu;

/** The positions inside a token where a break may fall: after a run of separators. */
const AFTER_SEPARATORS = /(?<=[._:=/\\@&?#+|])(?=[\p{L}\p{N}])/u;

/**
 * Adds a `<wbr>` at each break position inside every match of at least
 * {@link MIN_BREAKABLE_LENGTH} characters in a string: the shared work of
 * {@link withIdentifierBreaks} and {@link withTokenBreaks}, whose TSDoc states the contract.
 *
 * @param content - the text to render; any other node is returned as given
 * @param pattern - the global pattern of the runs that get breaks
 * @param breaks - the zero-width pattern of the break positions inside one run
 * @returns `content` unchanged, or one `<span>` of its text runs and `<wbr>` elements
 */
function withBreaks(content: ReactNode, pattern: RegExp, breaks: RegExp): ReactNode {
  if (typeof content !== 'string') {
    return content;
  }
  const parts: ReactNode[] = [];
  let run = '';
  let consumed = 0;
  for (const match of content.matchAll(pattern)) {
    const breakable = match[0];
    if (breakable.length < MIN_BREAKABLE_LENGTH) {
      continue;
    }
    const [first = '', ...rest] = breakable.split(breaks);
    run += content.slice(consumed, match.index) + first;
    for (const word of rest) {
      parts.push(run, <wbr key={`wbr-${parts.length}`} />);
      run = word;
    }
    consumed = match.index + breakable.length;
  }
  if (parts.length === 0) {
    return content;
  }
  parts.push(run + content.slice(consumed));
  return <span>{parts}</span>;
}

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
  return withBreaks(content, IDENTIFIER, AFTER_UNDERSCORES);
}

/**
 * Adds a line-break opportunity (`<wbr>`) after each run of separators inside every token of at
 * least sixteen characters in a string ({@link TOKEN}), so a line wraps between the parts of a
 * dataset name, URN, setting, path or address rather than inside one of its words.
 *
 * - Only a string is changed. Any other node (an element, a number, a list, `null`) is returned
 *   as given, and so is a string with no such token, so prose and short figures such as `9:30`
 *   or `EUR 240.00` render exactly as before.
 * - A changed string is returned as one `<span>` holding its text runs and `<wbr>` elements,
 *   which keeps them a single inline run in every container, a flex row included. The span has
 *   no role or attributes and `<wbr>` adds no character, so the text content, the accessible
 *   name and copied text are the string itself, exactly as the API wrote it.
 * - A part between two separators, such as `leadGenForm`, has no break of its own: where a
 *   container keeps words whole, as a `DataTable` cell does, the longest part sets its minimum
 *   width.
 *
 * @param content - the text to render, typically an API value or message in a table cell
 * @returns `content` unchanged, or the span with break opportunities
 *
 * @example
 * ```tsx
 * withTokenBreaks('hubspot.deal_search');
 * // <span>hubspot.<wbr />deal_<wbr />search</span>
 *
 * withTokenBreaks('Simulated (DATA_MODE=simulated)');
 * // <span>Simulated (DATA_<wbr />MODE=<wbr />simulated)</span>
 *
 * withTokenBreaks('Platform state at first sync');
 * // 'Platform state at first sync', unchanged: no token
 * ```
 */
export function withTokenBreaks(content: ReactNode): ReactNode {
  return withBreaks(content, TOKEN, AFTER_SEPARATORS);
}
