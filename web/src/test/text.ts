/**
 * Text readers for component tests.
 *
 * A `Tooltip` keeps its `role="tooltip"` bubble in the DOM while it is closed (only its `hidden`
 * attribute changes), so a label's `textContent` holds the label followed by its help text. A
 * browser shows the label alone; {@link textWithoutTooltips} reads what it shows, so a table
 * cell or list entry can be compared with its label exactly while the help text is tested on
 * its own, through the trigger's `aria-describedby`.
 *
 * `Banner` and `Badge` hold each standalone calendar date of a string title, content or label in
 * its own `ds-whole-date` span, so such text is split across elements and a plain string query
 * does not find it. {@link byTextContent} finds the element that holds the whole text instead.
 *
 * Only test files import this module; application code never does.
 *
 * @packageDocumentation
 */
import type { MatcherFunction } from '@testing-library/react';

/** The bubble every `Tooltip` renders (`ds-tooltip__content`, `role="tooltip"`). */
const TOOLTIP_SELECTOR = '[role="tooltip"]';

/**
 * Returns an element's text content without the text of any tooltip bubble inside it.
 *
 * The element itself is never modified. An element with no bubble inside it reads as its own
 * `textContent`, without a copy; one that holds a bubble is deep-cloned and the bubbles are
 * removed from the clone. An element that is itself a tooltip bubble reads as `''`.
 *
 * @param element - The element to read, such as a table cell or a list entry.
 * @returns The element's text content without tooltip help text.
 *
 * @example
 * ```ts
 * // <th><span class="ds-tooltip"><span …>Idle days</span><span role="tooltip" hidden>…</span>…
 * textWithoutTooltips(rowHeader); // "Idle days"
 * ```
 */
export function textWithoutTooltips(element: Element): string {
  if (element.matches(TOOLTIP_SELECTOR)) {
    return '';
  }
  // Most cells hold no tooltip; reading them directly skips a deep clone per cell.
  if (element.querySelector(TOOLTIP_SELECTOR) === null) {
    return element.textContent;
  }
  const copy = element.cloneNode(true);
  if (!(copy instanceof Element)) {
    return element.textContent;
  }
  for (const bubble of Array.from(copy.querySelectorAll(TOOLTIP_SELECTOR))) {
    bubble.remove();
  }
  return copy.textContent;
}

/**
 * A Testing Library text matcher for the element matching `selector` whose whole text content is
 * `text`, or contains a match of `text` when it is a regular expression.
 *
 * The default text matcher reads only an element's own text nodes, so it misses text that is
 * split across child elements, such as a banner title or chip label whose dates sit in
 * `ds-whole-date` spans. This one reads the element's full `textContent`, exactly as written (no
 * whitespace normalization), and `selector` names the element that holds the whole text, such
 * as `.ds-banner__title`, `.ds-banner__content` or `.ds-badge`. The query therefore returns that
 * element, the one a plain string query would return were the text a single node, and never an
 * ancestor or inner span that holds the same text. A regular expression is applied with
 * `String.prototype.search`, which ignores its `lastIndex`, so a global one is safe to reuse.
 *
 * @param selector - CSS selector of the element that holds the whole text.
 * @param text - The exact text content, or a pattern it must contain.
 * @returns A matcher for `getByText`, `queryByText`, `findByText` and their `All` variants.
 *
 * @example
 * ```ts
 * const title = 'Custom window 2026-09-26 – 2026-10-05';
 * within(banner).getByText(byTextContent('.ds-banner__title', title));
 * screen.getByText(byTextContent('.ds-badge', /^2026-09-08 – 2026-10-05 · 28 days/u));
 * ```
 */
export function byTextContent(selector: string, text: string | RegExp): MatcherFunction {
  return (_content, element) => {
    if (element === null || !element.matches(selector)) {
      return false;
    }
    const whole = element.textContent;
    return typeof text === 'string' ? whole === text : whole.search(text) >= 0;
  };
}
