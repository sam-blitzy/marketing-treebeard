/**
 * Sequential focus navigation (the Tab order) for the design-system wrappers that reason about
 * focus themselves, such as `Dialog`'s focus trap.
 *
 * This module is internal to `src/design-system/components`: it is not re-exported from the
 * design-system index, and feature code never imports it (AAP 0.4.2). Tab-order reasoning and
 * focus traps belong to the wrappers that own that behaviour, such as `Dialog`'s trap. Feature
 * code still moves focus to its own content (an error message after a failed save, a Retry
 * button, the control that opened a dialog) by calling `focus()` on those elements directly,
 * and needs none of these helpers.
 *
 * A tab stop is an element the browser visits with Tab:
 * - it is focusable: natively (`a[href]`, `area[href]`, `button`, `input` other than
 *   `type="hidden"`, `select`, `textarea`, `iframe`, `audio[controls]`, `video[controls]`, an
 *   editing host whose `contenteditable` is in its true or plaintext-only state and whose
 *   parent is not editable, and the first `summary` child of a `details`), or through a valid
 *   `tabindex` attribute. An editing host nested in editable content is not a stop of its own:
 *   the browser focuses the outermost host of the editable region;
 * - its effective tab index is 0 or more: a valid `tabindex` attribute wins, otherwise a
 *   natively focusable element counts as 0, whatever its `tabIndex` property reports (jsdom
 *   reports -1 for editing hosts, `audio` and `video`), and a negative value takes the element
 *   out of the order;
 * - it is not disabled (`:disabled`, which also covers the controls of a disabled `fieldset`),
 *   not inside an `inert` or `hidden` subtree, and not inside the content of a closed `details`
 *   (that element's first `summary` stays a stop);
 * - it is rendered: neither it nor any ancestor has `display: none`, no ancestor has
 *   `content-visibility: hidden`, and its own `visibility` is `visible`. An `area` has no box of
 *   its own, so the image that uses its map is judged instead.
 *
 * Rendering is judged by `Element.checkVisibility()` where the engine has it (every current
 * browser), and otherwise by walking `getComputedStyle()` up the ancestors: jsdom has no
 * `checkVisibility()`, and its computed `display` describes each element alone, so a child of a
 * `display: none` parent still reports its own value. Layout APIs (`offsetParent`,
 * `getClientRects()`) are never used, because jsdom computes no layout and they would hide every
 * element in component tests.
 *
 * The order is HTML's sequential navigation order: positive tab indexes first, ascending and in
 * document order among equals, then tab index 0 in document order. A named radio group is one
 * stop, its checked radio or else its first, as in the browser; arrow keys move within it.
 *
 * Some stops have internal navigation: Tab visits their parts before leaving them, while
 * `document.activeElement` stays the stop (the controls of `audio` and `video`, the fields of
 * a date or time `input`) or key events stop reaching the page (an `iframe`). Script cannot
 * follow those steps, so a focus trap leaves Tab to the browser wherever the browser's step
 * stays in order ({@link leavesTabToBrowser}), moves focus itself only at the trap's ends and
 * around positive tab indexes ({@link moveFocusWithin}), and catches focus that leaves a stop
 * with internal navigation at either end with focusable sentinels around its container. When
 * Shift+Tab wraps onto a last stop with internal navigation, the trap hands that step to the
 * browser from the trailing sentinel, so the stop is entered at its last part
 * ({@link shiftTabEntersLastStopFromEnd}).
 *
 * @packageDocumentation
 */

/** An element that can take focus: HTML and SVG elements both have `focus()` and `tabIndex`. */
export type FocusableElement = HTMLElement | SVGElement;

/** A tab stop and its effective tab index, which places it in the sequential order. */
interface TabStop {
  element: FocusableElement;
  tabIndex: number;
}

/**
 * Every element that may be a tab stop, before {@link tabStopsWithin} drops the ones that are
 * not: no effective tab index or a negative one, disabled, inert, hidden, not rendered, or a
 * radio that is not its group's stop.
 */
const candidateSelector = [
  'a[href]',
  'area[href]',
  'button',
  'input',
  'select',
  'textarea',
  'iframe',
  'audio[controls]',
  'video[controls]',
  '[contenteditable]',
  'details > summary:first-of-type',
  '[tabindex]',
].join(', ');

/**
 * A `tabindex` value that HTML's rules for parsing integers accept: optional ASCII whitespace,
 * an optional sign, then a digit. Any other value is ignored, as if the attribute were absent.
 */
const validTabIndex = /^[\t\n\f\r ]*[+-]?[0-9]/;

/** The `contenteditable` values, compared lowercase, that make an element an editing host. */
const editingHostStates = new Set(['', 'true', 'plaintext-only']);

/**
 * The `input` types whose control the browser splits into fields (day, month, year, hour and
 * so on) that Tab visits one by one before leaving the input.
 */
const segmentedInputTypes = new Set(['date', 'datetime-local', 'month', 'time', 'week']);

/**
 * Whether `element` can be focused by script: only HTML and SVG elements implement `focus()`.
 *
 * @param element - a candidate from the selector query
 * @returns `true` for an HTML or SVG element
 */
function isFocusableElement(element: Element): element is FocusableElement {
  return element instanceof HTMLElement || element instanceof SVGElement;
}

/**
 * The first `summary` child of a `details` element, which is its toggle and the only part of a
 * closed `details` that is rendered.
 *
 * @param details - a `details` element
 * @returns its first `summary` child, or `undefined` when it has none
 */
function toggleOf(details: Element): Element | undefined {
  return Array.from(details.children).find((child) => child.localName === 'summary');
}

/**
 * The state of the `contenteditable` attribute on `element` itself.
 *
 * @param element - the element to read
 * @returns `true` for the true and plaintext-only states, `false` for the false state, and
 *   `null` for the inherit state: no attribute, an invalid value, or an element that is not
 *   HTML (only HTML elements take the attribute)
 */
function editableStateOf(element: Element): boolean | null {
  const value = element instanceof HTMLElement ? element.getAttribute('contenteditable') : null;
  if (value === null) {
    return null;
  }
  const state = value.toLowerCase();
  if (editingHostStates.has(state)) {
    return true;
  }
  return state === 'false' ? false : null;
}

/**
 * Whether `element` is editable: the nearest element, from `element` up through its
 * ancestors, whose `contenteditable` is not in the inherit state is in the true or
 * plaintext-only state.
 *
 * @param element - the element to test, or `null` above the document element
 * @returns `false` when no such element exists or the nearest one is in the false state
 */
function isEditable(element: Element | null): boolean {
  for (let node = element; node !== null; node = node.parentElement) {
    const state = editableStateOf(node);
    if (state !== null) {
      return state;
    }
  }
  return false;
}

/**
 * Whether `element` is focusable without a `tabindex` attribute.
 *
 * @param element - a candidate from the selector query
 * @returns `true` for the natively focusable elements listed in the module documentation
 */
function isNativelyFocusable(element: Element): boolean {
  // Only the outermost editing host is a stop: an editing host inside editable content is
  // part of its ancestor's editing region, and browsers give that region one focus target.
  if (editableStateOf(element) === true) {
    return !isEditable(element.parentElement);
  }
  switch (element.localName) {
    case 'a':
    case 'area':
      return element.hasAttribute('href');
    case 'button':
    case 'select':
    case 'textarea':
    case 'iframe':
      return true;
    case 'input':
      return element instanceof HTMLInputElement && element.type !== 'hidden';
    case 'audio':
    case 'video':
      return element.hasAttribute('controls');
    case 'summary': {
      const parent = element.parentElement;
      return parent?.localName === 'details' && toggleOf(parent) === element;
    }
    default:
      return false;
  }
}

/**
 * The tab index that places `element` in the sequential order.
 *
 * @param element - a candidate from the selector query
 * @returns the value of a valid `tabindex` attribute; otherwise 0 for a natively focusable
 *   element and `null` for one that cannot take focus
 */
function tabIndexOf(element: Element): number | null {
  const attribute = element.getAttribute('tabindex');
  if (attribute !== null && validTabIndex.test(attribute)) {
    return Number.parseInt(attribute, 10);
  }
  return isNativelyFocusable(element) ? 0 : null;
}

/**
 * Whether `element` sits in the content of a closed `details`, at any depth. The first
 * `summary` of a closed `details`, and anything inside it, stays rendered.
 *
 * @param element - the element to test
 * @returns `true` when a closed `details` ancestor hides it
 */
function isInClosedDetails(element: Element): boolean {
  for (let ancestor = element.parentElement; ancestor !== null; ancestor = ancestor.parentElement) {
    if (ancestor.localName === 'details' && !ancestor.hasAttribute('open')) {
      const toggle = toggleOf(ancestor);
      if (toggle === undefined || !toggle.contains(element)) {
        return true;
      }
    }
  }
  return false;
}

/**
 * The parent `element` is rendered inside: its parent element, or its shadow host when it is a
 * shadow root's direct child.
 *
 * @param element - the element whose parent is wanted
 * @returns the parent, or `null` at the top of the document
 */
function renderParentOf(element: Element): Element | null {
  const parent = element.parentNode;
  return parent instanceof ShadowRoot ? parent.host : element.parentElement;
}

/**
 * Whether `element` is rendered and visible, as the module documentation defines it.
 *
 * @param element - an element that generates a box (not an `area`)
 * @returns `false` when it or an ancestor is not rendered, or when it is invisible
 */
function isRendered(element: Element): boolean {
  if (typeof element.checkVisibility === 'function') {
    return element.checkVisibility({ checkVisibilityCSS: true, visibilityProperty: true });
  }
  const own = getComputedStyle(element);
  if (own.visibility === 'hidden' || own.visibility === 'collapse') {
    return false;
  }
  for (let node: Element | null = element; node !== null; node = renderParentOf(node)) {
    const style = node === element ? own : getComputedStyle(node);
    if (style.display === 'none') {
      return false;
    }
    if (node !== element && style.getPropertyValue('content-visibility') === 'hidden') {
      return false;
    }
  }
  return true;
}

/**
 * Whether `element` is hidden from the user: inside an `inert` or `hidden` subtree, inside the
 * content of a closed `details`, or not rendered.
 *
 * @param element - an element that generates a box (not an `area`)
 * @returns `true` when the element cannot be seen or reached
 */
function isHidden(element: Element): boolean {
  return (
    element.closest('[inert], [hidden]') !== null ||
    isInClosedDetails(element) ||
    !isRendered(element)
  );
}

/**
 * The image an `area` belongs to: the first image whose `usemap` names the area's `map` by its
 * `name` or `id`. An `area` is rendered and reachable only through that image.
 *
 * @param area - an `area` element
 * @returns the image, or `null` when the area's map is used by none
 */
function imageOfArea(area: Element): HTMLImageElement | null {
  const map = area.closest('map');
  if (map === null) {
    return null;
  }
  const names = [map.getAttribute('name'), map.id].filter(
    (name): name is string => name !== null && name !== '',
  );
  return (
    Array.from(area.ownerDocument.images).find((image) => {
      const reference = image.getAttribute('usemap');
      return reference?.startsWith('#') === true && names.includes(reference.slice(1));
    }) ?? null
  );
}

/**
 * Whether a candidate with a non-negative tab index is reachable: not disabled and not hidden.
 *
 * @param element - a candidate from the selector query
 * @returns `true` when the browser would visit it with Tab
 */
function isReachable(element: FocusableElement): boolean {
  if (element.matches(':disabled')) {
    return false;
  }
  if (element.localName !== 'area') {
    return !isHidden(element);
  }
  const image = imageOfArea(element);
  return element.closest('[inert], [hidden]') === null && image !== null && !isHidden(image);
}

/**
 * Whether `element` is a radio button that belongs to a named group, which the browser treats
 * as one tab stop.
 *
 * @param element - a tab stop
 * @returns `true` for an `input type="radio"` with a non-empty `name`
 */
function isGroupedRadio(element: Element): element is HTMLInputElement {
  return element instanceof HTMLInputElement && element.type === 'radio' && element.name !== '';
}

/**
 * The tab stops inside `root`, in sequential navigation order, with their tab indexes.
 *
 * @param root - the element to search; `root` itself is never included
 * @returns the stops, possibly empty
 */
function tabStopsWithin(root: Element): TabStop[] {
  const stops: TabStop[] = [];
  // Radio groups are keyed by form owner, then name: radios in different forms never share a
  // group, even with the same name.
  const radioGroups = new Map<HTMLFormElement | null, Map<string, HTMLInputElement[]>>();
  for (const element of root.querySelectorAll(candidateSelector)) {
    if (!isFocusableElement(element)) {
      continue;
    }
    const tabIndex = tabIndexOf(element);
    if (tabIndex === null || tabIndex < 0 || !isReachable(element)) {
      continue;
    }
    if (isGroupedRadio(element)) {
      const byName = radioGroups.get(element.form) ?? new Map<string, HTMLInputElement[]>();
      radioGroups.set(element.form, byName);
      byName.set(element.name, [...(byName.get(element.name) ?? []), element]);
    }
    stops.push({ element, tabIndex });
  }
  const radioStops = new Set<Element>();
  for (const byName of radioGroups.values()) {
    for (const members of byName.values()) {
      const stop = members.find((member) => member.checked) ?? members[0];
      if (stop !== undefined) {
        radioStops.add(stop);
      }
    }
  }
  const kept = stops.filter(
    (stop) => !isGroupedRadio(stop.element) || radioStops.has(stop.element),
  );
  // Array.prototype.sort is stable, so equal positive indexes keep their document order.
  const positive = kept
    .filter((stop) => stop.tabIndex > 0)
    .sort((a, b) => a.tabIndex - b.tabIndex);
  return [...positive, ...kept.filter((stop) => stop.tabIndex === 0)];
}

/**
 * Returns the elements inside `root` that Tab visits, in the order it visits them, as the
 * module documentation defines them.
 *
 * @param root - the element to search; `root` itself is never included
 * @returns the tab stops of `root`, possibly empty
 */
export function tabbableWithin(root: Element): FocusableElement[] {
  return tabStopsWithin(root).map((stop) => stop.element);
}

/**
 * The index in `stops` where sequential navigation from `active` starts, before wrapping.
 *
 * @param stops - the tab stops of `root`, in order (not empty)
 * @param root - the container the stops belong to
 * @param active - the element that has focus
 * @param backward - `true` for Shift+Tab
 * @returns an index that may be -1 or `stops.length`, which wrap to the other end
 */
function startIndex(
  stops: readonly TabStop[],
  root: Element,
  active: Element | null,
  backward: boolean,
): number {
  // From outside root, or from root itself, navigation enters at the matching end.
  if (active === null || active === root || !root.contains(active)) {
    return backward ? stops.length - 1 : 0;
  }
  const position = stops.findIndex((stop) => stop.element === active);
  if (position !== -1) {
    return backward ? position - 1 : position + 1;
  }
  // An element inside root that is not a stop itself (tabindex="-1", a radio that is not its
  // group's stop) is a starting point at its place in document order among the tab index 0
  // stops, as in the browser.
  const following = stops.findIndex(
    (stop) =>
      stop.tabIndex === 0 &&
      (active.compareDocumentPosition(stop.element) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
  );
  const insertion = following === -1 ? stops.length : following;
  return backward ? insertion - 1 : insertion;
}

/**
 * Moves focus one step through the tab stops of `root`, as Tab (or Shift+Tab) would, wrapping
 * from the last stop to the first and back, so focus never leaves `root`.
 *
 * The step starts from the element that has focus. From outside `root`, or from `root` itself,
 * focus enters at the first stop (the last with `backward`). A stop that refuses focus, such
 * as one an engine cannot focus, is skipped in favour of the next one in the same direction.
 *
 * @param root - the container whose stops focus cycles through
 * @param backward - `true` to move as Shift+Tab does, `false` as Tab does
 * @returns `true` when a stop of `root` took focus; `false` when `root` has no stop that
 *   accepts focus, so the caller decides where focus goes
 */
export function moveFocusWithin(root: Element, backward: boolean): boolean {
  const stops = tabStopsWithin(root);
  if (stops.length === 0) {
    return false;
  }
  const { ownerDocument } = root;
  const start = startIndex(stops, root, ownerDocument.activeElement, backward);
  for (let step = 0; step < stops.length; step += 1) {
    const offset = backward ? start - step : start + step;
    const stop = stops[((offset % stops.length) + stops.length) % stops.length];
    if (stop === undefined) {
      continue;
    }
    stop.element.focus();
    const focused = ownerDocument.activeElement;
    if (focused !== null && stop.element.contains(focused)) {
      return true;
    }
  }
  return false;
}

/**
 * Whether the browser moves focus between parts of `element` with Tab before focus leaves
 * it, while `document.activeElement` stays `element` (or, for an iframe, key events stop
 * reaching this document): the play, time, volume and menu controls of `audio` or `video`
 * with `controls`, the content of an `iframe`, and the fields of a date or time `input`.
 * Script cannot tell which part has focus, so it cannot tell whether Tab leaves the element.
 *
 * @param element - a tab stop
 * @returns `true` for the elements listed above
 */
function hasInternalNavigation(element: Element): boolean {
  switch (element.localName) {
    case 'iframe':
      return true;
    case 'audio':
    case 'video':
      return element.hasAttribute('controls');
    case 'input':
      return element instanceof HTMLInputElement && segmentedInputTypes.has(element.type);
    default:
      return false;
  }
}

/**
 * Whether the Tab (or Shift+Tab) pressed now may be left to the browser by a caller that
 * keeps focus inside `root` with {@link moveFocusWithin}, so the browser's own sequential
 * navigation, inner stops included, is kept wherever it stays inside `root`.
 *
 * The browser's step is kept in two cases:
 * - between two stops with tab index 0: HTML orders them in document order, so the next one
 *   the browser visits is the next one in `root`, even past controls this module does not
 *   list (a keyboard-focusable scroll container, for example);
 * - from a stop with internal navigation (media controls, an iframe, a date or time input),
 *   whose inner parts the browser visits one by one but script cannot see. Leaving such a stop
 *   at either end of `root` takes focus past `root`'s edge, so the caller must catch it there,
 *   with a focusable sentinel on each side of `root` that hands focus back to the opposite end.
 *
 * @param root - the container focus must stay in
 * @param backward - `true` for Shift+Tab, `false` for Tab
 * @returns `false` when the caller must move focus itself (with {@link moveFocusWithin}): `root`
 *   has no tab stop; focus is outside `root` or on `root` itself; the step wraps past the last
 *   stop to the first or past the first to the last; or the step leaves or reaches a stop with
 *   a positive tab index, which HTML orders across the whole document rather than inside `root`
 */
export function leavesTabToBrowser(root: Element, backward: boolean): boolean {
  const active = root.ownerDocument.activeElement;
  if (active === null || active === root || !root.contains(active)) {
    return false;
  }
  const stops = tabStopsWithin(root);
  if (stops.length === 0) {
    return false;
  }
  const current = stops.find((stop) => stop.element === active);
  if (current !== undefined && hasInternalNavigation(current.element)) {
    return true;
  }
  // An element that is not a stop (tabindex="-1") steps like a tab index 0 stop at its place
  // in document order; startIndex places it the way the browser does.
  const target = stops[startIndex(stops, root, active, backward)];
  return target !== undefined && target.tabIndex === 0 && (current?.tabIndex ?? 0) === 0;
}

/**
 * Whether the Shift+Tab pressed now lands on the last stop of `root` (it wraps past the first
 * stop, or enters from `root` itself or from outside it), and that stop has internal
 * navigation and tab index 0. The browser enters such a stop backward at its last part (the
 * media menu button, the year field, the frame's last control), while focusing the stop by
 * script lands on its first part, so a caller that wants the browser's order hands this step
 * to the browser from an element placed right after `root`.
 *
 * @param root - the container focus must stay in
 * @returns `true` when the step should start from right after `root`
 */
export function shiftTabEntersLastStopFromEnd(root: Element): boolean {
  const stops = tabStopsWithin(root);
  const last = stops.at(-1);
  if (last === undefined || last.tabIndex !== 0 || !hasInternalNavigation(last.element)) {
    return false;
  }
  const start = startIndex(stops, root, root.ownerDocument.activeElement, true);
  return start === -1 || start === stops.length - 1;
}
