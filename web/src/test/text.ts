/**
 * Text readers for component tests.
 *
 * A `Tooltip` keeps its `role="tooltip"` bubble in the DOM while it is closed (only its `hidden`
 * attribute changes), so a label's `textContent` holds the label followed by its help text. A
 * browser shows the label alone; {@link textWithoutTooltips} reads what it shows, so a table
 * cell or list entry can be compared with its label exactly while the help text is tested on
 * its own, through the trigger's `aria-describedby`.
 *
 * Only test files import this module; application code never does.
 *
 * @packageDocumentation
 */

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
