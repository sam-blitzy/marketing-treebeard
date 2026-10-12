/**
 * Where keyboard focus goes on `/actions` once a card action, or the element that held focus,
 * has gone (WCAG 2.2 SC 2.4.3 Focus Order).
 *
 * A recorded feedback (dismiss, snooze, done) disables the footer action that held focus, and
 * the refetched recommendations usually remove its card: the section then shows the feedback's
 * confirmation where the card stood (`FeedbackConfirmation` in `./ActionOutcomes`). Focus goes,
 * in this order ({@link recoverFocus}):
 *
 * 1. to that confirmation, found by the recommendation identity it carries
 *    (`data-feedback-confirmation`), so focus and the outcome stay where the user acted;
 * 2. else, while the card is still on the page, to its own status line ("Marked done",
 *    "Dismissed", "Snoozed");
 * 3. else to the first enabled action of the next card row still on the page, else of the
 *    previous rows, nearest first, else to the heading of the nearest enclosing section still on
 *    the page, else to the page's `<main>`.
 *
 * The first two are revealed ({@link revealFocus}): focused without the browser's own scroll,
 * then scrolled into view only as far as needed (`block: 'nearest'`), an element taller than the
 * viewport with its top aligned to the viewport's (`block: 'start'`). A tall card that leaves
 * leaves its confirmation above the viewport at the scroll position the user acted at, and that
 * scroll brings it back into view, beside the cards that follow it. A confirmation that leaves the
 * page while it holds focus, as when the rerun that follows a feedback drops it, hands focus on
 * to the card that now stands in its place, revealed the same way ({@link handOffFocus}).
 *
 * Focus the user has moved elsewhere in the meantime is always left where it is
 * ({@link focusIsLostOrWithin}).
 *
 * The module imports nothing, so the card, the confirmation and the white-paper rows share it
 * without an import cycle.
 *
 * @packageDocumentation
 */

/**
 * Finds the action rows of the cards: `RecommendationActions` marks its row root with the
 * attribute `data-recommendation-actions`.
 */
const ACTIONS_ROW_SELECTOR = '[data-recommendation-actions]';

/**
 * The attribute a feedback confirmation carries its recommendation identity in
 * (`FeedbackConfirmation` in `./ActionOutcomes`).
 */
const CONFIRMATION_ATTRIBUTE = 'data-feedback-confirmation';

/** Finds every feedback confirmation, whatever identity it carries. */
const CONFIRMATION_SELECTOR = `[${CONFIRMATION_ATTRIBUTE}]`;

/**
 * Where focus was taken from, captured while the element and its neighbours are on the page
 * ({@link captureFocusOrigin}).
 */
export interface FocusOrigin {
  /** The element focus was in: a card's action row, or a feedback confirmation. */
  readonly element: HTMLElement;
  /** The element's ancestor `<section>` elements, nearest first. */
  readonly sections: readonly HTMLElement[];
  /**
   * The action rows after the element in document order, nearest first, inside the outermost
   * of those sections, or the whole document when there is none.
   */
  readonly following: readonly HTMLElement[];
  /** The action rows before the element in document order, nearest first, from the same scope. */
  readonly preceding: readonly HTMLElement[];
}

/** What a recorded feedback reveals before the action rows ({@link recoverFocus}). */
export interface FeedbackOutcomeTargets {
  /**
   * The recommendation identity of the item the feedback was recorded on (`SectionFrame`'s
   * `itemIdentity`): the feedback confirmation carrying it takes focus first.
   */
  readonly identity: string;
  /**
   * The card's own status line ("Marked done", "Dismissed", "Snoozed"), read when focus is
   * recovered: `null`, or an element no longer on the page, once the card has left.
   */
  readonly statusLine: () => HTMLElement | null;
}

/**
 * Captures where focus can go once `element` loses it: its enclosing sections and the action
 * rows around it.
 *
 * @param element - a card's action row on the press of one of its actions, or a feedback
 *   confirmation as it leaves the page; `null` before a row is attached
 * @returns the origin, or `null` without an element
 *
 * @example
 * ```ts
 * const origin = captureFocusOrigin(rowRef.current);
 * ```
 */
export function captureFocusOrigin(element: HTMLElement | null): FocusOrigin | null {
  if (element === null) {
    return null;
  }
  const sections: HTMLElement[] = [];
  for (let node = element.parentElement; node !== null; node = node.parentElement) {
    if (node.localName === 'section') {
      sections.push(node);
    }
  }
  const scope: ParentNode = sections.at(-1) ?? element.ownerDocument;
  const following: HTMLElement[] = [];
  const preceding: HTMLElement[] = [];
  for (const row of scope.querySelectorAll<HTMLElement>(ACTIONS_ROW_SELECTOR)) {
    if (row === element || row.contains(element) || element.contains(row)) {
      continue;
    }
    if ((element.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0) {
      following.push(row);
    } else {
      preceding.unshift(row);
    }
  }
  return { element, sections, following, preceding };
}

/**
 * Whether keyboard focus is lost or still inside `scope`: on no element, on the body (where a
 * browser leaves it once the focused button is disabled or removed), on an element no longer in
 * the document, or inside `scope`. Anywhere else the user has moved focus, and it stays there.
 *
 * @param scope - the element focus counts as still being in, such as a card's action row
 * @returns `true` when focus may be moved without taking it from where the user put it
 *
 * @example
 * ```ts
 * if (focusIsLostOrWithin(card)) {
 *   card.focus();
 * }
 * ```
 */
export function focusIsLostOrWithin(scope: Element): boolean {
  const { activeElement, body } = scope.ownerDocument;
  return (
    activeElement === null ||
    activeElement === body ||
    !activeElement.isConnected ||
    scope.contains(activeElement)
  );
}

/**
 * The first action of a row that can take focus: an enabled button, neither `disabled` nor
 * `aria-disabled="true"`, outside every `[hidden]` or `[inert]` subtree, such as a collapsed
 * held disclosure, an inactive tab panel or a collapsed table detail row.
 *
 * @param row - an action row
 * @returns the button, or `null` when the row offers none
 *
 * @example
 * ```ts
 * firstEnabledAction(row)?.focus();
 * ```
 */
export function firstEnabledAction(row: HTMLElement): HTMLButtonElement | null {
  const buttons = Array.from(row.querySelectorAll('button'));
  return (
    buttons.find(
      (button) =>
        !button.disabled &&
        button.getAttribute('aria-disabled') !== 'true' &&
        button.closest('[hidden], [inert]') === null,
    ) ?? null
  );
}

/**
 * The heading that names a section: the element its `aria-labelledby` points at.
 * `PageShell.Section` labels each section by its `${id}-title` heading.
 *
 * @param section - a `<section>` element
 * @returns the heading, or `null` when the section names none on the page
 */
function sectionHeading(section: HTMLElement): HTMLElement | null {
  const [id] = (section.getAttribute('aria-labelledby') ?? '').split(/\s+/u).filter(Boolean);
  return id === undefined ? null : section.ownerDocument.getElementById(id);
}

/**
 * Focuses `element`. With `fromScript`, an element without a `tabindex` of its own (a heading,
 * `<main>`, a status line) first gets `tabindex="-1"`, which lets script focus it without adding
 * a tab stop.
 *
 * @param element - the element to focus
 * @param fromScript - whether the element may need `tabindex="-1"` to take focus
 * @param options - passed to `focus()`, such as `{ preventScroll: true }`
 * @returns whether focus is now on `element`: a browser refuses focus to an element that is not
 *   rendered, which sends recovery on to the next candidate
 *
 * @example
 * ```ts
 * moveFocusTo(heading, true);
 * ```
 */
export function moveFocusTo(
  element: HTMLElement,
  fromScript = false,
  options?: FocusOptions,
): boolean {
  if (fromScript && !element.hasAttribute('tabindex')) {
    element.setAttribute('tabindex', '-1');
  }
  element.focus(options);
  return element.ownerDocument.activeElement === element;
}

/**
 * Focuses `element` where it stands and shows it: focus without the browser's own scroll, then
 * scrolls it into view only as far as needed (`block: 'nearest'`), so an element above or below
 * the viewport comes to its nearest edge and one already in view does not move. An element taller
 * than the viewport has its top aligned with the viewport's instead (`block: 'start'`): `nearest`
 * leaves one that already spans the viewport where it is, and brings one above it in only as far
 * as its bottom, so its heading would stay out of view. An element without a `tabindex` of its
 * own gets `tabindex="-1"` first ({@link moveFocusTo}).
 *
 * @param element - the element to reveal, such as a feedback confirmation or a card
 * @returns whether focus is now on `element`; an element refused focus is not scrolled to
 *
 * @example
 * ```ts
 * revealFocus(confirmation);
 * ```
 */
export function revealFocus(element: HTMLElement): boolean {
  if (!moveFocusTo(element, true, { preventScroll: true })) {
    return false;
  }
  // Environments without layout, such as jsdom, do not implement scrollIntoView.
  if (typeof element.scrollIntoView === 'function') {
    const root = element.ownerDocument.documentElement;
    const viewportHeight = root.clientHeight > 0 ? root.clientHeight : window.innerHeight;
    const taller = element.getBoundingClientRect().height > viewportHeight;
    element.scrollIntoView({ block: taller ? 'start' : 'nearest' });
  }
  return true;
}

/**
 * The feedback confirmation of `identity` on the page: inside the origin's outermost section
 * while it is on the page, else anywhere in the document, and outside every `[hidden]` or
 * `[inert]` subtree, such as a collapsed held disclosure. Each confirmation's attribute is
 * compared as a string, so an identity is never read as a CSS selector, whatever it holds.
 *
 * @param origin - where focus was taken from
 * @param identity - the recommendation identity the confirmation carries
 * @returns the confirmation, or `null` when the page shows none for it
 */
function findConfirmation(origin: FocusOrigin, identity: string): HTMLElement | null {
  const outermost = origin.sections.at(-1);
  const scope: ParentNode =
    outermost?.isConnected === true ? outermost : origin.element.ownerDocument;
  for (const confirmation of scope.querySelectorAll<HTMLElement>(CONFIRMATION_SELECTOR)) {
    if (
      confirmation.getAttribute(CONFIRMATION_ATTRIBUTE) === identity &&
      confirmation.closest('[hidden], [inert]') === null
    ) {
      return confirmation;
    }
  }
  return null;
}

/**
 * Gives keyboard focus a stable place once the element that had it, or held it, is disabled or
 * gone (see the module overview): for a recorded feedback (`outcome`), its confirmation, else
 * the card's status line, both revealed ({@link revealFocus}); then the first enabled action of
 * the next card row still on the page, else of the previous rows, nearest first, else the
 * heading of the nearest enclosing section still on the page, else the page's `<main>`. Rows are
 * looked for inside the outermost enclosing section only, so focus stays in the section the user
 * was working in.
 *
 * Nothing moves when focus is no longer lost or inside the origin's element
 * ({@link focusIsLostOrWithin}): the user has moved it, or it has already been handed on, such as
 * to a confirmation that took over the focus of the card it replaced.
 *
 * @param origin - what {@link captureFocusOrigin} captured
 * @param outcome - the recorded feedback's targets; omitted, only the action rows, headings and
 *   `<main>` are candidates
 *
 * @example
 * ```ts
 * window.setTimeout(() => {
 *   recoverFocus(origin, { identity, statusLine: () => statusRef.current });
 * }, 0);
 * ```
 */
export function recoverFocus(origin: FocusOrigin, outcome?: FeedbackOutcomeTargets): void {
  if (!focusIsLostOrWithin(origin.element)) {
    return;
  }
  if (outcome !== undefined) {
    const confirmation = findConfirmation(origin, outcome.identity);
    if (confirmation !== null && revealFocus(confirmation)) {
      return;
    }
    const statusLine = outcome.statusLine();
    if (statusLine?.isConnected === true && revealFocus(statusLine)) {
      return;
    }
  }
  focusAround(origin, (row) => {
    const action = firstEnabledAction(row);
    return action !== null && moveFocusTo(action);
  });
}

/**
 * Hands on the keyboard focus a feedback confirmation held as it left the page, as when the
 * rerun that follows the feedback no longer lists its recommendation, to what stands in its place
 * now: the card of the next action row still on the page, else of the previous rows, nearest
 * first, focused on its `<article>` and revealed ({@link revealFocus}). The page does not jump to
 * a tall card's footer, and no action of a card the user did not choose waits on Enter; the next
 * Tab enters that card. A row outside every card, such as a white-paper row's details, offers its
 * first enabled action the same way. Rows inside a `[hidden]` or `[inert]` subtree are skipped.
 * Without any, the heading of the nearest enclosing section still on the page takes focus, else
 * the page's `<main>`.
 *
 * Nothing moves when focus is no longer lost or inside the origin's element
 * ({@link focusIsLostOrWithin}), such as once a card of the same recommendation mounting in the
 * confirmation's place has taken focus over (`./carriedFocus`).
 *
 * @param origin - what {@link captureFocusOrigin} captured from the confirmation as it left
 *
 * @example
 * ```ts
 * window.setTimeout(() => {
 *   handOffFocus(origin);
 * }, 0);
 * ```
 */
export function handOffFocus(origin: FocusOrigin): void {
  if (!focusIsLostOrWithin(origin.element)) {
    return;
  }
  focusAround(origin, (row) => {
    if (row.closest('[hidden], [inert]') !== null) {
      return false;
    }
    const card = row.closest('article');
    if (card !== null) {
      return revealFocus(card);
    }
    const action = firstEnabledAction(row);
    return action !== null && revealFocus(action);
  });
}

/**
 * Moves focus near the origin: into the first action row, after it then before it, nearest first,
 * that `focusRow` focuses; else onto the heading of the nearest enclosing section still on the
 * page; else onto the page's `<main>`.
 *
 * @param origin - where focus was taken from
 * @param focusRow - focuses a target in a row still on the page; returns whether focus is there
 */
function focusAround(origin: FocusOrigin, focusRow: (row: HTMLElement) => boolean): void {
  const { element, sections, following, preceding } = origin;
  for (const candidate of [...following, ...preceding]) {
    if (candidate.isConnected && focusRow(candidate)) {
      return;
    }
  }
  for (const section of sections) {
    const heading = section.isConnected ? sectionHeading(section) : null;
    if (heading !== null && moveFocusTo(heading, true)) {
      return;
    }
  }
  const main = element.ownerDocument.querySelector('main');
  if (main !== null) {
    moveFocusTo(main, true);
  }
}
