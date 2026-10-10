/**
 * Focus helpers for in-page navigation (WCAG 2.2 SC 2.4.3 Focus Order): reading the element id a
 * URL fragment names, and moving keyboard focus to an element that may not be focusable by
 * itself, such as a section region or a card's `<article>`.
 *
 * The root route focuses the target of a same-document link with them (`src/router.tsx`), and
 * the bounded batches of `/actions` focus the entry a reveal or a hash change shows
 * (`features/actions/batches.tsx`). The module imports nothing, so any module can use it
 * without an import cycle.
 *
 * @packageDocumentation
 */

/**
 * The element id a URL fragment names: the fragment without its leading `#`, percent-decoded.
 * A fragment that is not valid percent-encoding, such as `%E0%A4%A`, is used as written, which
 * is the id the browser itself would look up.
 *
 * @param hash - a fragment as `location.hash` and `URL.hash` spell it: empty, or `#` followed by
 *   the fragment; a fragment without its `#` is read the same way
 * @returns the id, or `null` when the fragment is empty (`''` or `'#'`)
 *
 * @example
 * ```ts
 * idFromHash('#section-keywords'); // "section-keywords"
 * idFromHash('#item-%37%35'); // "item-75"
 * idFromHash('#'); // null
 * ```
 */
export function idFromHash(hash: string): string | null {
  const fragment = hash.startsWith('#') ? hash.slice(1) : hash;
  if (fragment === '') {
    return null;
  }
  try {
    return decodeURIComponent(fragment);
  } catch {
    // decodeURIComponent throws URIError on a malformed escape; the raw fragment stands.
    return fragment;
  }
}

/**
 * Moves keyboard focus to `element`.
 *
 * An element that is not focusable by itself (no `tabindex` attribute and a negative
 * `tabIndex`, such as a `<section>`, an `<article>` or a heading) first gets `tabindex="-1"`,
 * which lets script focus it without making it a Tab stop. The attribute is removed again when
 * the element loses focus, or at once when the browser refuses it focus (an element that is not
 * rendered), so the element is left exactly as it was rendered. An element that is focusable
 * natively or through a `tabindex` of its own is focused as it is.
 *
 * Once it holds focus, the next Tab reaches the first tab stop after it in document order: the
 * first control inside a region.
 *
 * @param element - the element to focus
 * @param options - passed to `focus()`, for example `{ preventScroll: true }` when the element
 *   has already been scrolled into view
 * @returns whether `element` now has focus
 *
 * @example
 * ```ts
 * const section = document.getElementById('section-keywords');
 * if (section !== null) {
 *   focusElement(section, { preventScroll: true });
 * }
 * ```
 */
export function focusElement(element: HTMLElement, options?: FocusOptions): boolean {
  if (element.hasAttribute('tabindex') || element.tabIndex >= 0) {
    element.focus(options);
    return element.ownerDocument.activeElement === element;
  }
  const restore = (): void => {
    element.removeAttribute('tabindex');
  };
  element.setAttribute('tabindex', '-1');
  element.addEventListener('blur', restore, { once: true });
  element.focus(options);
  if (element.ownerDocument.activeElement === element) {
    return true;
  }
  element.removeEventListener('blur', restore);
  restore();
  return false;
}
