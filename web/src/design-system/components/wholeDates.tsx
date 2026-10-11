/**
 * Whole calendar dates in wrapped text, such as the custom-window banner title
 * "Custom window 2026-08-15 – 2026-10-09".
 *
 * Daily dates are shown as stored, `YYYY-MM-DD` with ASCII hyphens (AAP 0.5.5). Browsers treat
 * each of those hyphens as a line-break opportunity, so on a narrow screen a title could otherwise
 * break inside its end date ("Custom window 2026-08-15 – 2026-10-" / "09").
 * {@link withWholeDates} holds each standalone date in a `ds-whole-date` span, whose
 * `white-space: nowrap` (`wholeDates.css`) keeps it on one line. The spaces and the en dash
 * between dates stay break opportunities, so a range wraps between its dates. No character is
 * added or replaced, so the text content, the accessible name and copied text keep the stored
 * dates exactly.
 *
 * `Banner` applies it to a string title or content and `Badge` to a string label, composed with
 * `withIdentifierBreaks` (`identifierBreaks.tsx`) for the text between dates, so every banner
 * and chip gets both. The design-system index exports it for dated text that feature code
 * places elsewhere.
 *
 * GAP: keep applying it in the Blitzy OS banner and chip bindings, unless Blitzy OS keeps such
 * dates whole itself (`../GAPS.md`).
 *
 * @packageDocumentation
 */
import './wholeDates.css';

import { Fragment, type ReactNode } from 'react';

/**
 * A standalone calendar date: four, two and two ASCII digits joined by hyphens, with no letter,
 * digit, underscore or hyphen of any script directly before or after it.
 *
 * The boundaries leave alone what only looks like a date: a date inside an identifier
 * (`snapshot_2026-10-09`, `2026-10-09_backfill`), so identifier breaks see that text exactly as
 * they would without this helper; an ISO datetime (`2026-10-11T05:01:10Z`); and a longer run of
 * digits and hyphens (`12026-10-09`, `2026-10-091`, `2026-10-09-01`). Spaces, punctuation,
 * brackets and the en dash of a range may sit on either side.
 */
const STANDALONE_DATE = /(?<![\p{L}\p{N}_-])\d{4}-\d{2}-\d{2}(?![\p{L}\p{N}_-])/gu;

/**
 * Renders plain text unchanged: the default `renderText` of {@link withWholeDates}.
 *
 * @param text - a run of text
 * @returns `text` itself
 */
function asIs(text: string): ReactNode {
  return text;
}

/**
 * Holds each standalone calendar date of a string in a `<span className="ds-whole-date">`, so a
 * line never breaks inside a date, only around it.
 *
 * - Only a string is changed. Any other node (an element, a number, a list, `null`) is returned
 *   as given.
 * - A string with no standalone date is returned as `renderText(content)`, which is the string
 *   itself by default, so dateless text renders exactly as it would without this helper.
 * - A string with dates is returned as one `<span>` holding, in order, each run of text between
 *   dates passed through `renderText` and each date in its `ds-whole-date` span. The outer span
 *   keeps them one inline run in every container, a flex container such as the `Badge` chip
 *   included. Neither span has a role or other attributes, so the text content and the
 *   accessible name are the string itself.
 *
 * @param content - the text to render, typically a title or label that quotes stored dates
 * @param renderText - renders each run of text outside the dates; defaults to the run itself.
 *   `Banner` and `Badge` pass `withIdentifierBreaks`. A date is never next to a letter,
 *   digit or underscore, so an identifier always lies whole inside one run.
 * @returns `content` unchanged, `renderText(content)`, or the span holding whole dates
 *
 * @example
 * ```tsx
 * withWholeDates('Custom window 2026-08-15 – 2026-10-09');
 * // <span>Custom window <span className="ds-whole-date">2026-08-15</span> – <span
 * //   className="ds-whole-date">2026-10-09</span></span>
 *
 * withWholeDates('HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured', withIdentifierBreaks);
 * // withIdentifierBreaks('HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured'): no date
 *
 * withWholeDates('Synced 2026-10-11T05:01:10Z');
 * // 'Synced 2026-10-11T05:01:10Z', unchanged: a datetime, not a standalone date
 * ```
 */
export function withWholeDates(
  content: ReactNode,
  renderText: (text: string) => ReactNode = asIs,
): ReactNode {
  if (typeof content !== 'string') {
    return content;
  }
  const parts: ReactNode[] = [];
  let consumed = 0;
  for (const match of content.matchAll(STANDALONE_DATE)) {
    const date = match[0];
    const before = content.slice(consumed, match.index);
    if (before !== '') {
      parts.push(<Fragment key={`text-${parts.length}`}>{renderText(before)}</Fragment>);
    }
    parts.push(
      <span key={`date-${parts.length}`} className="ds-whole-date">
        {date}
      </span>,
    );
    consumed = match.index + date.length;
  }
  if (parts.length === 0) {
    return renderText(content);
  }
  const after = content.slice(consumed);
  if (after !== '') {
    parts.push(<Fragment key={`text-${parts.length}`}>{renderText(after)}</Fragment>);
  }
  return <span>{parts}</span>;
}
