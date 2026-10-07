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
 *   div.ds-dialog__backdrop[aria-hidden]       a click on it closes the dialog
 *   div.ds-dialog__panel[role=dialog][aria-modal=true][aria-labelledby][tabindex=-1]
 *     div.ds-dialog__header
 *       h2.ds-dialog__title[id]                the dialog's accessible name
 *       Button (quiet, sm)                     "Close" by default
 *     div.ds-dialog__body                      the caller's content
 *     div.ds-dialog__footer                    only when a footer is given
 * ```
 *
 * Accessibility (WCAG 2.2 AA, AAP 0.1.1), implemented without a headless library (AAP 0.4.5):
 * - 4.1.2 Name, Role, Value: `role="dialog"` with `aria-modal="true"`, named by its visible
 *   title through `aria-labelledby`, so `getByRole('dialog', { name: 'Record decision' })`
 *   matches and assistive technology treats the page behind it as inert.
 * - 2.4.3 Focus Order: on open, focus moves to the first focusable element of the body, so a
 *   form's first field is ready for input; when the body holds none, the panel itself takes
 *   focus (it has `tabindex="-1"` and a `:focus-visible` ring in `Dialog.css`). On close,
 *   focus returns to the element that was focused when the dialog opened, normally its opener.
 * - 2.1.1 Keyboard and 2.1.2 No Keyboard Trap: Tab and Shift+Tab cycle through the panel's
 *   focusable elements, the Close button included, and wrap at either end, so focus never
 *   reaches the page behind the modal. The modal is never a trap: Escape, the Close button and
 *   a backdrop click all close it.
 *
 * Why not `HTMLDialogElement.showModal()`: jsdom does not implement it, so component tests of
 * every feature that opens a dialog would fail. Modal behaviour is therefore implemented here
 * with ARIA, a portal and a keyboard handler, which behave identically in browsers and jsdom.
 * For the same reason the focusable-element query never filters by layout (`offsetParent` is
 * always `null` and `getClientRects()` is empty in jsdom) and nothing calls `scrollIntoView`
 * (not implemented in jsdom); hidden content is detected through the `hidden` and `inert`
 * attributes instead.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: semantic HTML styled solely by semantic tokens. Binding Blitzy OS
 * later changes only this file and `Dialog.css`, never the feature code that uses it
 * (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Dialog.css';

import { useEffect, useId, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { Button } from './Button';

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
   * accessible name, for example "Record decision".
   */
  title: ReactNode;
  /**
   * Called when the user asks to close the dialog: Escape inside the panel, the Close button
   * or a click on the backdrop. The parent must respond by setting `open` to `false`; the
   * dialog stays open otherwise, so a parent may keep it open, for example while a submit is
   * pending.
   */
  onClose: () => void;
  /**
   * The dialog's content, such as `DecisionForm`. Its first focusable element takes focus when
   * the dialog opens.
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
 * Elements that can take keyboard focus, before the attribute filters in
 * {@link focusableWithin} remove hidden, inert and disabled ones.
 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/**
 * Returns the elements inside `root` that are reachable with Tab, in DOM order, which is the
 * order Tab visits them.
 *
 * An element is kept when it is an `HTMLElement` (so it can be focused), is not inside a
 * `hidden` or `inert` subtree, is not disabled (a disabled control that also carries a
 * `tabindex`, or one inside a disabled fieldset, matches the selector but cannot be focused),
 * and has a non-negative `tabIndex`. Visibility is judged from attributes only, never from
 * layout, because jsdom computes no layout (see the module documentation).
 *
 * @param root the element to search; `root` itself is never included
 * @returns the focusable descendants of `root`, possibly empty
 */
function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
    (element): element is HTMLElement =>
      element instanceof HTMLElement &&
      element.closest('[hidden], [inert]') === null &&
      !element.matches(':disabled') &&
      element.tabIndex >= 0,
  );
}

/**
 * Modal dialog: renders the panel into `document.body` while `open` is `true`, moves focus
 * into it, keeps Tab and Shift+Tab inside it, closes on Escape, the Close button or a backdrop
 * click (by calling `onClose`), and returns focus to the previously focused element when it
 * closes or unmounts.
 *
 * Usage contract:
 * - The parent owns `open`, and `onClose` must set it to `false`.
 * - Only one dialog is open at a time. Dialogs are not stacked: each one traps focus within
 *   its own panel and covers the whole viewport.
 * - Escape is handled on the panel and does not propagate further, so a page-level Escape
 *   handler (such as a tooltip's) never fires while the dialog handles it.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * const [open, setOpen] = useState(false);
 *
 * <Button variant="primary" onClick={() => setOpen(true)}>Record decision</Button>
 * <Dialog
 *   open={open}
 *   title="Record decision"
 *   onClose={() => setOpen(false)}
 *   footer={<Button type="submit" form="decision-form" variant="primary">Save decision</Button>}
 * >
 *   <DecisionForm id="decision-form" subject={subject} onSaved={() => setOpen(false)} />
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
  const panelRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Focus management, keyed on `open` alone so a re-render of an open dialog (new children or
  // a new onClose) never steals focus back to the first field. The effect runs after the
  // portal is committed, so both refs are attached when it starts, and at that moment focus
  // has not moved yet: document.activeElement is still the opener. The cleanup runs when the
  // dialog closes or unmounts, after the panel has left the DOM, and restores focus to the
  // opener if it is still in the document. Under React StrictMode's mount, unmount and remount
  // cycle the cleanup restores the opener first, so the remount stores the same opener again.
  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const body = bodyRef.current;
    const initial = (body === null ? undefined : focusableWithin(body)[0]) ?? panelRef.current;
    initial?.focus();
    return () => {
      if (previous?.isConnected === true) {
        previous.focus();
      }
    };
  }, [open]);

  if (!open) {
    return null;
  }

  /**
   * Keyboard handling on the panel: Escape closes; Tab and Shift+Tab wrap at the ends of the
   * panel's focusable elements. Between the ends the browser moves focus natively, so only
   * the wrap cases call `preventDefault`.
   */
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== 'Tab') {
      return;
    }
    const panel = panelRef.current;
    if (panel === null) {
      return;
    }
    const focusables = focusableWithin(panel);
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (first === undefined || last === undefined) {
      // Nothing inside is focusable: the panel keeps focus instead of letting it leave.
      event.preventDefault();
      panel.focus();
      return;
    }
    const active = document.activeElement;
    // Focus outside the panel's DOM can only come from content portalled out of the dialog by
    // a descendant component; Tab from there re-enters the panel at the matching end.
    const outside = active === null || !panel.contains(active);
    if (event.shiftKey) {
      if (active === first || active === panel || outside) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || outside) {
      event.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <div className="ds-dialog">
      <div className="ds-dialog__backdrop" aria-hidden="true" onClick={onClose} />
      <div
        ref={panelRef}
        className="ds-dialog__panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
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
    </div>,
    document.body,
  );
}
