/**
 * `Dialog`: the product's accessible modal dialog (AAP 0.4.2, "Form controls (login, decision
 * form): `Dialog`", status GAP).
 *
 * Hosts `DecisionForm` for "Record decision" and "Record intended state" (AAP 0.5.5) and any
 * other modal form. Feature code renders it through the design-system index and never builds a
 * modal of its own (AAP 0.4.4).
 *
 * Rendered structure, portalled to `document.body` and matching the class contract in
 * `Dialog.css`:
 *
 * ```text
 * div.ds-dialog
 *   span.ds-visually-hidden[tabindex=0]        leading focus sentinel
 *   div.ds-dialog__backdrop[aria-hidden]       a click on it closes the dialog
 *   div.ds-dialog__panel[role=dialog][aria-modal=true][aria-labelledby][tabindex=-1]
 *     div.ds-dialog__header
 *       h2.ds-dialog__title[id]                the dialog's accessible name
 *       Button (quiet, sm)                     "Close" by default
 *     div.ds-dialog__body                      the caller's content
 *     div.ds-dialog__footer                    only when a footer is given
 *   span.ds-visually-hidden[tabindex=0]        trailing focus sentinel
 * ```
 *
 * The sentinels are empty, have no role and are clipped by `ds-visually-hidden` from
 * `shared.css`. They are focusable, so they are not `aria-hidden`; they only ever hold focus
 * for the instant it takes to send it back into the panel.
 *
 * Accessibility (WCAG 2.2 AA, AAP 0.1.1), implemented without a headless library (AAP 0.4.5):
 * - 4.1.2 Name, Role, Value: `role="dialog"` with `aria-modal="true"`, named by its visible
 *   title through `aria-labelledby`, so `getByRole('dialog', { name: 'Record decision' })`
 *   matches and assistive technology treats the page behind it as inert; the page is also made
 *   inert in the DOM (below). The title is required plain text and an open dialog with a blank
 *   one throws, so the dialog is never unnamed.
 * - 2.4.3 Focus Order: on open, focus moves to the first tab stop of the body, so a form's
 *   first field is ready for input; when the body holds none, the panel itself takes focus (it
 *   has `tabindex="-1"` and a `:focus-visible` ring in `Dialog.css`). A hidden, disabled or
 *   inert element is never a tab stop, and a descendant that takes focus with `autoFocus`
 *   keeps it. On close or unmount, focus returns to the element that was focused when the
 *   dialog opened, normally its opener; it is recorded while rendering the opening, before any
 *   descendant's `autoFocus` moves focus.
 * - 2.1.1 Keyboard and 2.1.2 No Keyboard Trap: Tab and Shift+Tab move through the panel's tab
 *   stops, the Close button included, in the browser's sequential order, and wrap at either
 *   end. Between the ends the browser moves focus itself, so it also visits the parts of a
 *   control that has its own stops: the play, time, volume and menu controls of `audio` or
 *   `video` with `controls`, the fields of a date or time input, the content of an iframe.
 *   The dialog moves focus only where the browser's step would leave the panel or the order:
 *   Tab on the last stop goes to the first, Shift+Tab on the first stop (or on the panel
 *   itself) goes to the last, Tab from content a descendant portals outside the panel enters at
 *   the matching end, a step to or from a positive `tabindex` follows the panel's own order,
 *   and with no stop at all the panel keeps focus. A stop with parts of its own is left to the
 *   browser even at an end, because script cannot see which part has focus: Tab past its last
 *   part reaches the trailing sentinel, which sends focus to the panel's first stop, and
 *   Shift+Tab past its first part reaches the leading sentinel, which sends focus to the last
 *   stop. When Shift+Tab wraps onto a last stop with parts of its own, the dialog puts focus on
 *   the trailing sentinel and lets the browser take the step from there, so that stop is
 *   entered at its last part, as the browser enters it from after it. The leading sentinel
 *   comes before the backdrop, so after a click on the backdrop of a dialog its parent keeps
 *   open, Tab enters at the first stop and Shift+Tab at the last. A document focus guard
 *   returns focus that reaches the page behind the modal by any other path (a script focusing
 *   an element there, or a positive `tabindex` there) to the first stop. The modal is never a
 *   trap: Escape, the Close button and a backdrop click all close it.
 * - Modal background: while the dialog is open, the page behind it is `inert` and the document
 *   does not scroll. Every element child of `document.body` at the moment the dialog opens (the
 *   app's root among them) gets the `inert` attribute, so nothing there can be clicked, focused
 *   or found by assistive technology; the dialog's own portal root, and content its descendants
 *   portal to `document.body`, are added to the body later in the commit that opens it, so they
 *   stay interactive. An element that already has `inert` is left alone and keeps it after the
 *   dialog closes. `<html>` takes the `ds-dialog-scroll-lock` class, which `Dialog.css` styles
 *   with `overflow: hidden`, so wheel, touch, keyboard and scrollbar scrolling of the page stop
 *   while the scroll position is kept, and the panel still scrolls its own content. When the
 *   page shows a classic vertical scrollbar as the dialog opens, `<html>` also takes the
 *   `ds-dialog-scroll-lock--gutter` modifier (`scrollbar-gutter: stable`), so the content behind
 *   the backdrop does not shift as the scrollbar hides. On close or unmount both are removed
 *   before focus returns to the opener, because an element inside an inert subtree cannot take
 *   focus. The attributes and classes are counted per element across open dialogs, so when one
 *   dialog closes in the commit that opens another, the page stays inert and locked throughout.
 *
 * Tab stops follow `./focusable.ts`: natively focusable elements (links, form controls,
 * iframes, media with controls, editing hosts, a `details` toggle) and valid `tabindex`
 * values, in tab index order, with one stop per radio group; disabled, `inert`, `hidden`,
 * closed-`details` and unrendered content (`display: none` on the element or an ancestor,
 * `visibility: hidden`, `content-visibility: hidden`) is skipped.
 *
 * Why not `HTMLDialogElement.showModal()`: jsdom does not implement it, so component tests of
 * every feature that opens a dialog would fail. Modal behaviour is therefore implemented here
 * with ARIA, a portal and a keyboard handler, which behave identically in browsers and jsdom.
 * For the same reason rendering is judged by `Element.checkVisibility()` where the engine has
 * it and by computed styles otherwise (jsdom), never by layout (`offsetParent` is always
 * `null` and `getClientRects()` is empty in jsdom), and nothing calls `scrollIntoView` (not
 * implemented in jsdom). Component tests therefore need no visibility mocks.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: semantic HTML styled by semantic tokens, except for the title's
 * `flex: 1 1 auto` factor and the sentinels' visually hidden `1px` box and `inset(50%)` clip
 * that `../GAPS.md` lists. Binding Blitzy OS later changes only this file, `./focusable.ts` and
 * `Dialog.css`, never the feature code that uses it (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './shared.css';
import './Dialog.css';

import { useEffect, useId, useRef, useState } from 'react';
import type { FocusEvent, KeyboardEvent, ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { Button } from './Button';
import { leavesTabToBrowser, moveFocusWithin, shiftTabEntersLastStopFromEnd } from './focusable';
import type { FocusableElement } from './focusable';

/**
 * Props of {@link Dialog}.
 *
 * The interface is closed on purpose: there is no `className` or `style` prop, because feature
 * code must not restyle a wrapper (AAP 0.4.4). The panel's size, spacing and layering come only
 * from `Dialog.css`.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS modal.
 */
export interface DialogProps {
  /**
   * Whether the dialog is shown. The parent owns this state: the dialog never closes itself,
   * it asks the parent to through `onClose`. While `false`, nothing is rendered.
   */
  open: boolean;
  /**
   * The visible title, rendered as the panel's level-2 heading and used as the dialog's
   * accessible name, for example "Record decision". It is plain text, so the visible title and
   * the name assistive technology announces are always the same words.
   *
   * It must contain visible text: an open dialog whose title is empty or whitespace only would
   * have no accessible name, so rendering one throws an `Error` instead (a programming error
   * that surfaces in tests, never an unnamed modal).
   */
  title: string;
  /**
   * Called when the user asks to close the dialog: Escape inside the panel, the Close button
   * or a click on the backdrop. The parent must respond by setting `open` to `false`; the
   * dialog stays open otherwise, so a parent may keep it open, for example while a submit is
   * pending.
   */
  onClose: () => void;
  /**
   * The dialog's content, such as `DecisionForm`. Its first tab stop takes focus when the
   * dialog opens, unless a descendant takes focus itself with `autoFocus`.
   */
  children: ReactNode;
  /**
   * Optional row of actions below the content, such as the form's submit and cancel buttons.
   * When omitted (or `null`), no footer row is rendered.
   */
  footer?: ReactNode;
  /** The Close button's label, which is also its accessible name. Defaults to `'Close'`. */
  closeLabel?: string;
}

/**
 * The element that has focus now, as a target to return focus to.
 *
 * @returns the focused HTML or SVG element, or `null` when no element has focus
 */
function focusedElement(): FocusableElement | null {
  const active = document.activeElement;
  return active instanceof HTMLElement || active instanceof SVGElement ? active : null;
}

/**
 * The page behind a dialog that is opening: the element children of `document.body` now. It is
 * read while rendering the opening, as the return focus target is, before the commit that adds
 * the dialog's portal root and any content its descendants portal to `document.body`, so those
 * are never part of it.
 *
 * @returns the body's element children, in document order
 */
function bodyChildren(): readonly Element[] {
  return Array.from(document.body.children);
}

/**
 * The background elements open dialogs have made inert, each with the number of open dialogs
 * holding it. An element enters only when a dialog finds it without `inert`, so an `inert` that
 * anything else set is never removed, and it leaves when its last holder closes, so a dialog
 * closing in the commit that opens another never frees the page the other one covers.
 */
const inertHolds = new Map<Element, number>();

/** The number of open dialogs holding the document scroll lock. */
let scrollLockHolds = 0;

/** The class on `<html>` that stops the document scrolling while a dialog is open (`Dialog.css`). */
const SCROLL_LOCK_CLASS = 'ds-dialog-scroll-lock';

/**
 * The modifier on `<html>` that keeps the space of the page's vertical scrollbar while the scroll
 * lock hides it (`Dialog.css`), so nothing behind the backdrop shifts.
 */
const SCROLL_LOCK_GUTTER_CLASS = 'ds-dialog-scroll-lock--gutter';

/**
 * Makes the page behind an open dialog inert and stops the document scrolling, until the
 * returned release runs.
 *
 * Each element of `background` that is still a child of `document.body`, other than the
 * dialog's `root` and any element containing it, gets the `inert` attribute. It is set and
 * removed as an attribute, which every engine reflects (jsdom included), rather than through the
 * `inert` property. An element that already has `inert` from anything but an open dialog is
 * skipped and never touched. The first hold adds the scroll-lock class to `<html>`, together
 * with the gutter modifier when the page shows a classic vertical scrollbar at that moment (the
 * viewport is wider than the root's client area); measuring before the lock is applied keeps a
 * page without one, or with overlay scrollbars, from gaining a gutter it never had.
 *
 * @param background - the body children recorded when the dialog opened ({@link bodyChildren})
 * @param root - the dialog's portal root, which stays interactive
 * @returns the release, to be called once, which removes this hold's share of the `inert`
 *   attributes and of the scroll lock
 */
function holdModalBackground(background: readonly Element[], root: Element): () => void {
  const held: Element[] = [];
  for (const element of background) {
    if (element.parentElement !== document.body || element === root || element.contains(root)) {
      continue;
    }
    const holds = inertHolds.get(element);
    if (holds !== undefined) {
      inertHolds.set(element, holds + 1);
    } else if (element.hasAttribute('inert')) {
      continue;
    } else {
      element.setAttribute('inert', '');
      inertHolds.set(element, 1);
    }
    held.push(element);
  }
  const html = document.documentElement;
  if (scrollLockHolds === 0) {
    html.classList.toggle(SCROLL_LOCK_GUTTER_CLASS, window.innerWidth > html.clientWidth);
    html.classList.add(SCROLL_LOCK_CLASS);
  }
  scrollLockHolds += 1;
  return () => {
    for (const element of held) {
      const holds = inertHolds.get(element) ?? 0;
      if (holds > 1) {
        inertHolds.set(element, holds - 1);
      } else {
        inertHolds.delete(element);
        element.removeAttribute('inert');
      }
    }
    scrollLockHolds -= 1;
    if (scrollLockHolds === 0) {
      html.classList.remove(SCROLL_LOCK_CLASS, SCROLL_LOCK_GUTTER_CLASS);
    }
  };
}

/**
 * Modal dialog: renders the panel into `document.body` while `open` is `true`, moves focus
 * into it, makes the page behind it inert and stops the page scrolling, keeps Tab and
 * Shift+Tab inside it, closes on Escape, the Close button or a backdrop click (by calling
 * `onClose`), and once it closes or unmounts frees the page, at the scroll position it had, and
 * returns focus to the element that had focus when it opened.
 *
 * Usage contract:
 * - The parent owns `open`, and `onClose` must set it to `false`.
 * - `title` is non-blank text; an open dialog with a blank title throws rather than render
 *   without an accessible name.
 * - Only one dialog is open at a time. Dialogs are not stacked: each one traps focus within
 *   its own panel and covers the whole viewport.
 * - The page behind is what `document.body` holds when the dialog opens. Content a descendant
 *   portals to `document.body` stays interactive; anything else added to the body while the
 *   dialog is open is not made inert, and the focus guard still sends focus there back to the
 *   panel.
 * - Escape on the panel closes the dialog and stops propagating there. An open `Tooltip`
 *   inside the dialog consumes the first Escape in the capture phase, before it reaches the
 *   panel, so that press only dismisses the tooltip and the dialog stays open.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * `DecisionForm` renders its own "Save decision" and "Cancel" buttons, so its dialog takes no
 * `footer`; a footer is for content whose actions sit outside the body.
 *
 * ```tsx
 * const [open, setOpen] = useState(false);
 *
 * <Button variant="primary" onClick={() => setOpen(true)}>Record decision</Button>
 * <Dialog open={open} title="Record decision" onClose={() => setOpen(false)}>
 *   <DecisionForm
 *     subject={subject}
 *     onDone={() => setOpen(false)}
 *     onCancel={() => setOpen(false)}
 *   />
 * </Dialog>
 * ```
 */
export function Dialog({
  open,
  title,
  onClose,
  children,
  footer,
  closeLabel = 'Close',
}: DialogProps) {
  // Hooks run unconditionally, before the early return for a closed dialog.
  const titleId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const leadingSentinelRef = useRef<HTMLSpanElement>(null);
  const trailingSentinelRef = useRef<HTMLSpanElement>(null);
  const treeFocusRef = useRef<EventTarget | null>(null);
  // The fallback timer of a Shift+Tab handed to the browser from the trailing sentinel, while
  // that hand-off is pending; the focus guard leaves the sentinel's focus alone meanwhile.
  const handoffTimerRef = useRef<number | null>(null);

  // The element focus returns to on close: the one that had focus when `open` turned true,
  // normally the opener. It is read while rendering the opening, because a descendant with
  // `autoFocus` takes focus while React commits the panel, before any effect of this component
  // runs (a layout effect included), so an effect would read that descendant instead. This is
  // React's pattern for adjusting state when a prop changes: the update made during render is
  // applied before React renders the children, and it is dropped with any render React
  // discards. The page behind the dialog, which it makes inert while open, is read in the same
  // render, before the commit adds the dialog's own portal root to the body.
  const [returnFocus, setReturnFocus] = useState<FocusableElement | null>(() =>
    open ? focusedElement() : null,
  );
  const [background, setBackground] = useState<readonly Element[]>(() =>
    open ? bodyChildren() : [],
  );
  const [openedWith, setOpenedWith] = useState(open);
  if (open !== openedWith) {
    setOpenedWith(open);
    setReturnFocus(open ? focusedElement() : null);
    setBackground(open ? bodyChildren() : []);
  }
  // The element that had focus inside the panel when a cleanup ran while the panel stayed in
  // the document, as React StrictMode's simulated unmount does; the setup that follows gives it
  // focus back, so the remount keeps a descendant's `autoFocus` or the user's place.
  const resumeFocusRef = useRef<FocusableElement | null>(null);

  // Focus management, keyed on `open` and the return target captured with it, so a re-render
  // of an open dialog (new children or a new onClose) never steals focus back to the first
  // field. The effect runs after the portal is committed, so every ref is attached when it
  // starts. Focus already inside the panel (a descendant's `autoFocus`) stays where it is;
  // otherwise the body's first tab stop, or the panel, takes it. Then the page behind the
  // dialog turns inert and stops scrolling, once focus has left it. The cleanup runs when the
  // dialog closes or unmounts, after the panel has left the DOM, frees the page, and returns
  // focus to `returnFocus` if it is still in the document.
  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const root = rootRef.current;
    const panel = panelRef.current;
    const body = bodyRef.current;
    const leadingSentinel = leadingSentinelRef.current;
    const trailingSentinel = trailingSentinelRef.current;
    if (
      root === null ||
      panel === null ||
      body === null ||
      leadingSentinel === null ||
      trailingSentinel === null
    ) {
      return undefined;
    }
    const resume = resumeFocusRef.current;
    resumeFocusRef.current = null;
    const active = document.activeElement;
    if (active === null || !panel.contains(active)) {
      if (resume?.isConnected === true && panel.contains(resume)) {
        resume.focus();
      } else if (!moveFocusWithin(body, false)) {
        panel.focus();
      }
    }
    // Focus is inside the panel now, so no element of the page holds focus as it turns inert.
    const releaseBackground = holdModalBackground(background, root);
    // Focus guard and sentinels: focus that reaches an element outside the panel by a path
    // the keyboard handler leaves to the browser or never sees is sent back into the panel.
    // Focus on the leading sentinel comes from Shift+Tab out of the panel's first stop, or into
    // the panel from before it (after a backdrop click), so it goes to the last stop. Focus on
    // the trailing sentinel (Tab past the last stop, out of an iframe or media controls) and on
    // any element of the page behind the modal (a script focusing it, or a positive `tabindex`
    // there) goes to the first stop. With no stop, the panel takes it. Content a descendant
    // portals outside the panel is part of the dialog and keeps its focus (`treeFocusRef`, set
    // by React before this document listener runs). An event whose target no longer has focus
    // is stale: focus moved again while it was dispatched, and that later move had its own
    // event. The trailing sentinel keeps focus while a Shift+Tab is handed to the browser from
    // it (`handoffTimerRef`, see onKeyDown). The flag stops two guards from bouncing focus
    // between each other without end.
    let redirecting = false;
    const onDocumentFocusIn = (event: Event) => {
      const { target } = event;
      if (
        redirecting ||
        !(target instanceof Node) ||
        target !== document.activeElement ||
        panel.contains(target) ||
        target === treeFocusRef.current ||
        (target === trailingSentinel && handoffTimerRef.current !== null)
      ) {
        return;
      }
      redirecting = true;
      try {
        if (!moveFocusWithin(panel, target === leadingSentinel)) {
          panel.focus();
        }
      } finally {
        redirecting = false;
      }
    };
    document.addEventListener('focusin', onDocumentFocusIn);
    return () => {
      // The guard goes first, so returning focus outside the panel is never sent back in.
      document.removeEventListener('focusin', onDocumentFocusIn);
      if (handoffTimerRef.current !== null) {
        window.clearTimeout(handoffTimerRef.current);
        handoffTimerRef.current = null;
      }
      const focused = focusedElement();
      resumeFocusRef.current = focused !== null && panel.contains(focused) ? focused : null;
      // The page is freed before focus returns: an element inside an inert subtree cannot take
      // focus, and the opener is normally part of the page behind the dialog.
      releaseBackground();
      if (returnFocus?.isConnected === true) {
        returnFocus.focus();
      }
    };
  }, [open, returnFocus, background]);

  if (!open) {
    return null;
  }
  // The type already requires a string; the runtime check also covers untyped callers.
  if (typeof title !== 'string' || title.trim() === '') {
    throw new Error(
      'Dialog: `title` must contain visible text. It is the level-2 heading that names the ' +
        'dialog for assistive technology (WCAG 2.2 SC 4.1.2), so an open dialog never renders ' +
        'with an empty or whitespace-only title.',
    );
  }

  /**
   * Keyboard handling on the panel: Escape closes. Tab and Shift+Tab are left to the browser
   * wherever its own step stays inside the panel and in order (`leavesTabToBrowser`); where it
   * would not, at either end, from portalled content or the panel itself, or around a positive
   * `tabindex`, focus moves here to the next or previous tab stop of the panel instead, wrapping
   * at either end. A Shift+Tab that lands on a last stop with parts of its own
   * (`shiftTabEntersLastStopFromEnd`) moves focus to the trailing sentinel and keeps the
   * browser's default action, which then steps back from there into that stop's last part; a
   * timer moves focus to the stop itself should the browser not take that step. A Tab that a
   * descendant already handled (`preventDefault`), and Tab with Alt, Ctrl or Meta, which are
   * browser and system shortcuts rather than sequential navigation, are left alone.
   */
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose();
      return;
    }
    if (
      event.key !== 'Tab' ||
      event.defaultPrevented ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey
    ) {
      return;
    }
    const panel = panelRef.current;
    if (panel === null || leavesTabToBrowser(panel, event.shiftKey)) {
      return;
    }
    const trailingSentinel = trailingSentinelRef.current;
    if (event.shiftKey && trailingSentinel !== null && shiftTabEntersLastStopFromEnd(panel)) {
      // The browser's default action starts from the element focused when it runs, which is
      // after every keydown handler has returned; the timer runs in a later task.
      if (handoffTimerRef.current !== null) {
        window.clearTimeout(handoffTimerRef.current);
      }
      handoffTimerRef.current = window.setTimeout(() => {
        handoffTimerRef.current = null;
        if (document.activeElement === trailingSentinel && !moveFocusWithin(panel, true)) {
          panel.focus();
        }
      }, 0);
      trailingSentinel.focus();
      return;
    }
    event.preventDefault();
    // Focus outside the panel's DOM can only come from content portalled out of the dialog by
    // a descendant component; moveFocusWithin re-enters the panel at the matching end from
    // there.
    if (!moveFocusWithin(panel, event.shiftKey)) {
      // Nothing inside accepts focus: the panel keeps focus instead of letting it leave.
      panel.focus();
    }
  }

  /**
   * Records the target of every focus event that reaches the panel through React. React
   * propagates events through the component tree, so this includes content a descendant
   * portals outside the panel's DOM, which the document focus guard must leave alone.
   */
  function onFocus(event: FocusEvent<HTMLDivElement>) {
    treeFocusRef.current = event.target;
  }

  return createPortal(
    <div ref={rootRef} className="ds-dialog">
      {/* Focus sentinels: the document focus guard in the effect above sends focus that lands
          on one back into the panel, at its last stop from this one and at its first stop from
          the trailing one. This one precedes the backdrop, so after a click on the backdrop
          (which starts the browser's next Tab from there) Tab enters the panel at its first
          stop and Shift+Tab reaches this sentinel, as from anywhere before the panel. */}
      <span ref={leadingSentinelRef} className="ds-visually-hidden" tabIndex={0} />
      <div className="ds-dialog__backdrop" aria-hidden="true" onClick={onClose} />
      <div
        ref={panelRef}
        className="ds-dialog__panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        onFocus={onFocus}
      >
        <div className="ds-dialog__header">
          <h2 id={titleId} className="ds-dialog__title">
            {title}
          </h2>
          <Button variant="quiet" size="sm" onClick={onClose}>
            {closeLabel}
          </Button>
        </div>
        <div ref={bodyRef} className="ds-dialog__body">
          {children}
        </div>
        {/* A null footer is treated like an omitted one, so no empty row takes up a gap. */}
        {footer !== undefined && footer !== null ? (
          <div className="ds-dialog__footer">{footer}</div>
        ) : null}
      </div>
      <span ref={trailingSentinelRef} className="ds-visually-hidden" tabIndex={0} />
    </div>,
    document.body,
  );
}
