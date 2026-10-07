/**
 * `Tooltip`: the product's help-text wrapper for metrics and labels (AAP 0.4.2, status GAP).
 *
 * Shows a short description beside a trigger while the trigger is hovered or focused. The
 * description is plain help text, never an action: a tooltip holds no interactive content, so
 * anything a user must click belongs in a `Dialog` or on the card itself.
 *
 * Accessibility (WCAG 2.2 AA, AAP 0.1.1), implemented without a headless library (AAP 0.4.5):
 * - 4.1.2 Name, Role, Value: the bubble has `role="tooltip"` and the trigger references it
 *   through `aria-describedby`. The bubble stays in the DOM while closed (only its `hidden`
 *   attribute changes), so the reference always resolves and assistive technology reads the
 *   description whether or not the bubble is shown.
 * - 2.1.1 Keyboard: the trigger is focusable, and focus opens the bubble exactly as hover does.
 * - 1.4.13 Content on Hover or Focus:
 *   - dismissible: Escape hides the bubble without moving focus or pointer, both when focus is
 *     inside the tooltip and, through a document listener, when it was opened by hover alone;
 *   - hoverable: the hover handlers sit on the wrapper that contains the bubble, and the
 *     stylesheet bridges the gap between trigger and bubble, so the pointer can move onto the
 *     bubble without closing it;
 *   - persistent: the bubble stays open until hover and focus have both ended or the user
 *     presses Escape. Nothing hides it on a timer.
 *
 * Rendered structure, matching the class contract in `Tooltip.css`:
 *
 * ```html
 * <span class="ds-tooltip">
 *   <!-- the child element, given aria-describedby, or for a string child: -->
 *   <span class="ds-tooltip__trigger" tabindex="0" aria-describedby="…">text</span>
 *   <span class="ds-tooltip__content" role="tooltip" id="…" hidden>help text</span>
 * </span>
 * ```
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: semantic HTML styled solely by semantic tokens. Binding Blitzy OS
 * later changes only this file and `Tooltip.css`, never the feature code that uses it
 * (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Tooltip.css';

import { cloneElement, isValidElement, useEffect, useId, useState } from 'react';
import type {
  FocusEventHandler,
  KeyboardEventHandler,
  MouseEventHandler,
  ReactElement,
  ReactNode,
} from 'react';

/**
 * Props of {@link Tooltip}.
 *
 * The interface is closed on purpose: there is no `className`, `style` or placement prop, so a
 * caller cannot restyle the wrapper (AAP 0.4.4). The bubble always opens below the trigger,
 * aligned to its inline start, as `Tooltip.css` lays it out.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS tooltip.
 */
export interface TooltipProps {
  /**
   * The help text. Keep it short and non-interactive: it is read as the trigger's accessible
   * description, and a tooltip never receives focus itself. When the content is empty
   * (`null`, `undefined`, a boolean or `''`), no tooltip is rendered and the children are
   * returned unchanged, so an optional help string never produces an empty bubble.
   */
  content: ReactNode;
  /**
   * The trigger.
   *
   * - An element child must be focusable and must pass `aria-describedby` through to its
   *   focusable DOM element, as `Button` does. The tooltip's id is appended to any
   *   `aria-describedby` the element already carries, so existing descriptions are kept.
   * - A string child is wrapped in a focusable `<span class="ds-tooltip__trigger">` with
   *   `tabindex="0"`, so a plain metric label can carry help text and still be reached by
   *   keyboard.
   */
  children: ReactElement | string;
}

/** Props the trigger element receives from {@link Tooltip}. */
interface DescribedProps {
  'aria-describedby'?: string;
}

/**
 * Appends `id` to a space-separated id reference list, keeping every existing id once.
 *
 * @param existing - the trigger's current `aria-describedby`, if any
 * @param id - the tooltip's id
 * @returns the merged list, with `id` last and no duplicate or empty entries
 */
function mergeIds(existing: string | undefined, id: string): string {
  const ids = (existing ?? '').split(/\s+/).filter((entry) => entry !== '' && entry !== id);
  ids.push(id);
  return ids.join(' ');
}

/**
 * Whether an event's related target (where focus came from or is going to) lies inside
 * `container`. `null` (focus entering from or leaving to outside the document) and non-node
 * targets count as outside.
 *
 * @param container - the tooltip wrapper
 * @param target - the event's `relatedTarget`
 * @returns `true` when `target` is `container` or one of its descendants
 */
function isInside(container: Element, target: EventTarget | null): boolean {
  return target instanceof Node && container.contains(target);
}

/**
 * Whether `content` renders anything worth a bubble. React renders `null`, `undefined` and
 * booleans as nothing, and an empty string has no text.
 *
 * @param content - the tooltip's content prop
 * @returns `false` for content that would render an empty bubble
 */
function hasContent(content: ReactNode): boolean {
  return (
    content !== null && content !== undefined && typeof content !== 'boolean' && content !== ''
  );
}

/**
 * Attaches help text to a trigger, shown while the trigger is hovered or focused and
 * dismissible with Escape.
 *
 * Escape that dismisses an open tooltip stops there: its propagation is stopped, so a
 * surrounding `Dialog` does not also close. Escape while the tooltip is closed propagates
 * normally.
 *
 * The bubble is positioned by `Tooltip.css` relative to the wrapper and is not portalled, so a
 * container that clips its overflow also clips the bubble; place tooltips where the bubble has
 * room below the trigger.
 *
 * @example
 * ```tsx
 * <Tooltip content="Spend divided by Qualified Leads, within one currency.">
 *   Cost per Qualified Lead
 * </Tooltip>
 *
 * <Tooltip content="Requests a sync of every dataset for this source.">
 *   <Button variant="secondary" onClick={requestSync}>Sync now</Button>
 * </Tooltip>
 * ```
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @param props - see {@link TooltipProps}
 * @returns the wrapper holding the trigger and its `role="tooltip"` bubble, or the children
 *   unchanged when `content` is empty
 */
export function Tooltip({ content, children }: TooltipProps) {
  const tooltipId = useId();

  // Hover and focus are tracked separately rather than as one open flag (WCAG 2.2 SC 1.4.13,
  // persistent): the bubble stays open while either trigger is still present, so moving the
  // pointer away from a keyboard-focused trigger, or blurring a hovered one, never hides it.
  // `dismissed` records an Escape until the next hover or focus starts.
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const enabled = hasContent(content);
  const open = enabled && (hovered || focused) && !dismissed;

  // A tooltip opened by hover alone has no focus inside it, so the wrapper never sees the
  // key. While open, Escape anywhere in the document dismisses it (SC 1.4.13, dismissible).
  useEffect(() => {
    if (!open) {
      return;
    }
    const onDocumentKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        setDismissed(true);
      }
    };
    document.addEventListener('keydown', onDocumentKeyDown);
    return () => {
      document.removeEventListener('keydown', onDocumentKeyDown);
    };
  }, [open]);

  if (!enabled) {
    return <>{children}</>;
  }

  const onMouseEnter: MouseEventHandler<HTMLSpanElement> = () => {
    setHovered(true);
    setDismissed(false);
  };

  const onMouseLeave: MouseEventHandler<HTMLSpanElement> = () => {
    setHovered(false);
  };

  // React's onFocus and onBlur bubble (focusin and focusout), so focus moving between two
  // elements inside the wrapper fires both; only focus crossing the wrapper's edge counts.
  const onFocus: FocusEventHandler<HTMLSpanElement> = (event) => {
    if (isInside(event.currentTarget, event.relatedTarget)) {
      return;
    }
    setFocused(true);
    setDismissed(false);
  };

  const onBlur: FocusEventHandler<HTMLSpanElement> = (event) => {
    if (isInside(event.currentTarget, event.relatedTarget)) {
      return;
    }
    setFocused(false);
  };

  const onKeyDown: KeyboardEventHandler<HTMLSpanElement> = (event) => {
    if (event.key === 'Escape' && open) {
      // This Escape only dismisses the tooltip: stopping it here keeps an enclosing Dialog,
      // whose own Escape handler sits further up the tree, open.
      event.stopPropagation();
      setDismissed(true);
    }
  };

  // A string is never a valid element, so it takes the focusable-span branch; so would any
  // other non-element a JavaScript caller passes, which keeps every trigger keyboard-reachable.
  const trigger = isValidElement<DescribedProps>(children) ? (
    cloneElement(children, {
      'aria-describedby': mergeIds(children.props['aria-describedby'], tooltipId),
    })
  ) : (
    <span className="ds-tooltip__trigger" tabIndex={0} aria-describedby={tooltipId}>
      {children}
    </span>
  );

  return (
    <span
      className="ds-tooltip"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onFocus={onFocus}
      onBlur={onBlur}
      onKeyDown={onKeyDown}
    >
      {trigger}
      <span className="ds-tooltip__content" role="tooltip" id={tooltipId} hidden={!open}>
        {content}
      </span>
    </span>
  );
}
