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
 * - 2.1.1 Keyboard: every trigger has a keyboard-focusable element that the bubble describes,
 *   either the child's own (a button or field control) that the browser actually visits with
 *   Tab, or a focusable trigger span around a child that has none (see
 *   `TooltipProps.children`), and focus opens the bubble exactly as hover does.
 * - 1.4.13 Content on Hover or Focus:
 *   - dismissible: Escape hides the bubble without moving focus or pointer, whether focus is
 *     inside the tooltip or it was opened by hover alone, through a capture-phase document
 *     listener that consumes that press before an enclosing `Dialog` sees it;
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
 *   <!-- an element child with its own focusable, described element (a button or a field): -->
 *   <span><button aria-describedby="… tooltip-id">…</button></span>
 *   <!-- or a string child, or an element child with no such element: -->
 *   <span class="ds-tooltip__trigger" tabindex="0" aria-describedby="tooltip-id">…</span>
 *   <span class="ds-tooltip__content" role="tooltip" id="tooltip-id" hidden>help text</span>
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

import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import type { FocusEventHandler, MouseEventHandler, ReactElement, ReactNode } from 'react';

import { hasContent, mergeIdRefs } from './a11y';
import { tabbableWithin } from './focusable';
import type { FocusableElement } from './focusable';

/**
 * Props of {@link Tooltip}.
 *
 * The interface is closed on purpose: there is no `className`, `style` or placement prop, so a
 * caller cannot restyle the wrapper (AAP 0.4.4). Placement is automatic: the bubble opens
 * below the trigger, aligned to its inline start, flips above it when there is not enough room
 * below and more above, and shifts along the inline axis to stay inside the viewport (see
 * {@link Tooltip}).
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS tooltip.
 */
export interface TooltipProps {
  /**
   * The help text. Keep it short and non-interactive: it is read as the trigger's accessible
   * description, and a tooltip never receives focus itself. When the content is empty
   * (`null`, `undefined`, a boolean or `''`), no tooltip is rendered and the children are
   * returned unchanged (no bubble, no `aria-describedby`, no extra focus stop), so an optional
   * help string never produces an empty bubble. Hover and focus state is cleared at that
   * moment, so content that returns later starts closed.
   */
  content: ReactNode;
  /**
   * The trigger. Every trigger ends up with a keyboard-reachable element that the tooltip
   * describes:
   *
   * - An element child receives the tooltip's id in its `aria-describedby`, after the ids it
   *   already carries (each id once). When, after rendering, an element inside it that the
   *   browser visits with Tab carries that id, as with a raw `<button>`, a `Button`, or a
   *   `TextField` or `Select` (which forward `aria-describedby` to their control), that element
   *   is the trigger and no focus stop is added.
   * - Otherwise the child is shown inside a focusable `<span class="ds-tooltip__trigger">`
   *   with `tabindex="0"` and the tooltip's id, so the help stays reachable by keyboard. This
   *   covers a plain element such as a `<span>`, a disabled or loading `Button`, a disabled
   *   field, a composite component that drops `aria-describedby` instead of passing it to its
   *   focusable element, and one that passes it to an element Tab never reaches: one that is
   *   not rendered (`display: none` from an inline style or a stylesheet), `hidden`, `inert`,
   *   or inside a closed `details`. The check is repeated whenever the child's DOM or its
   *   rendering changes, so a `Button` that becomes disabled or hidden gains the span and loses
   *   it again once enabled or shown, without being remounted; if the span has focus at that
   *   moment, focus moves to the child. Inside a hidden container (a collapsed row, a hidden
   *   tab panel) nothing is reachable, so the span is present while the container is hidden
   *   and is removed again once it is revealed and the child is reachable, leaving one
   *   described stop.
   * - A string child is always shown inside that focusable span, so a plain metric label can
   *   carry help text and still be reached by keyboard.
   */
  children: ReactElement | string;
}

/** Props the trigger element receives from {@link Tooltip}. */
interface DescribedProps {
  'aria-describedby'?: string;
}

/**
 * The attributes whose change, on the trigger or anywhere inside it, can change which element
 * Tab reaches there, whether that element is rendered, or whether it carries the tooltip's id
 * (see `./focusable` for what makes a tab stop); a change to any of them re-runs the trigger
 * check. `class` and `style` cover stylesheet rules and inline styles that hide or show an
 * element; `open` covers a `details` element that hides its content while closed; `name`,
 * `form` and `checked` change which radio of a group is the stop; `id`, `name` and `usemap`
 * tie an `area` to the image whose rendering decides it.
 */
const triggerAttributes = [
  'aria-describedby',
  'checked',
  'class',
  'contenteditable',
  'controls',
  'disabled',
  'form',
  'hidden',
  'href',
  'id',
  'inert',
  'name',
  'open',
  'popover',
  'style',
  'tabindex',
  'type',
  'usemap',
];

/**
 * Finds the element inside `trigger` that keyboard users reach and that the tooltip
 * describes: one of the trigger's tab stops, as the browser renders them, whose
 * `aria-describedby` lists `tooltipId`. A descendant that is disabled, `inert`, `hidden`, in a
 * closed `details`, or not rendered (`display: none` from an inline style or a stylesheet, on
 * it or an ancestor) is not a tab stop, so it never counts as the trigger even when it carries
 * the id. `trigger` itself is never a candidate, so the result does not depend on whether the
 * fallback span is focusable, and adding or removing that focus stop cannot flip it.
 *
 * Concealment outside `trigger` (the whole tooltip inside a hidden container) leaves no tab
 * stop either, so the fallback span is added while concealed; the reveal is a rendering change
 * that re-runs this check (see {@link Tooltip}), which then finds the child and removes it.
 *
 * @param trigger - the span that holds the child
 * @param tooltipId - the bubble's id
 * @returns the first such element in Tab order, or `null` when there is none
 */
function describedTabStop(trigger: HTMLElement, tooltipId: string): FocusableElement | null {
  return (
    tabbableWithin(trigger).find((element) =>
      (element.getAttribute('aria-describedby') ?? '').split(/\s+/).includes(tooltipId),
    ) ?? null
  );
}

/**
 * Whether a `toggle` event's target can change the rendering of the trigger: a `details` or
 * popover element that contains the tooltip, or one inside it.
 *
 * @param wrapper - the tooltip wrapper
 * @param target - the event's target
 * @returns `true` when `target` is an ancestor or a descendant of `wrapper`
 */
function isRelatedToggle(wrapper: Element, target: EventTarget | null): boolean {
  return target instanceof Node && (target.contains(wrapper) || wrapper.contains(target));
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
 * Reads a computed length such as `'8px'` as a number of CSS pixels.
 *
 * @param value - a computed style value
 * @param fallback - the result for a value that is not a length, such as `'none'` or the
 *   empty string jsdom returns for a property no stylesheet sets
 * @returns the length in pixels, or `fallback`
 */
function pixels(value: string, fallback: number): number {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/**
 * Places the open bubble in the viewport with `position: fixed`, so no ancestor that clips or
 * scrolls its overflow can cut it off, against the wrapper's rectangle:
 *
 * 1. The bubble is narrowed to the viewport's inline extent less its inline margins when that
 *    is smaller than the stylesheet's `max-inline-size`, so it never overflows a narrow
 *    viewport.
 * 2. It opens below the trigger, unless the room below is smaller than the bubble with its
 *    block margins and the room above is larger; then it opens above (`data-placement="top"`,
 *    which moves the hover bridge in `Tooltip.css` to the bubble's lower edge).
 * 3. Its inline start is aligned with the trigger's (the left edge, or the right edge when the
 *    computed `direction` is `rtl`), then shifted so the bubble and its inline margins stay
 *    inside the viewport.
 *
 * The gap to the trigger and the distance from the viewport edges are the bubble's block and
 * inline margins, read from its computed style, so both come from the tokens in
 * `Tooltip.css`. The viewport extent excludes a scrollbar (`clientWidth`), falling back to
 * `innerWidth` where no layout exists (jsdom reports `0`); there, every rectangle is empty and
 * the bubble is simply placed at the top-left, with no error.
 *
 * @param anchor - the tooltip wrapper, whose box is the trigger's rectangle
 * @param bubble - the open `role="tooltip"` element
 */
function placeBubble(anchor: HTMLElement, bubble: HTMLElement): void {
  const root = document.documentElement;
  const viewportWidth = root.clientWidth > 0 ? root.clientWidth : window.innerWidth;
  const viewportHeight = root.clientHeight > 0 ? root.clientHeight : window.innerHeight;

  // Read the stylesheet's own maximum before an earlier placement's inline limit overrides it.
  bubble.style.maxWidth = '';
  const style = getComputedStyle(bubble);
  const marginTop = pixels(style.marginTop, 0);
  const marginRight = pixels(style.marginRight, 0);
  const marginBottom = pixels(style.marginBottom, 0);
  const marginLeft = pixels(style.marginLeft, 0);
  const fit = Math.max(0, viewportWidth - marginLeft - marginRight);
  if (fit < pixels(style.maxWidth, Number.POSITIVE_INFINITY)) {
    bubble.style.maxWidth = `${String(fit)}px`;
  }

  const size = bubble.getBoundingClientRect();
  const trigger = anchor.getBoundingClientRect();
  const outerWidth = marginLeft + size.width + marginRight;
  const outerHeight = marginTop + size.height + marginBottom;

  const roomBelow = viewportHeight - trigger.bottom;
  const roomAbove = trigger.top;
  const placement = roomBelow < outerHeight && roomAbove > roomBelow ? 'top' : 'bottom';
  // `top` and `left` position the margin box, so the margins themselves form the gaps.
  const top = placement === 'top' ? trigger.top - outerHeight : trigger.bottom;
  const start =
    style.direction === 'rtl' ? trigger.right - size.width - marginLeft : trigger.left - marginLeft;
  const left = Math.max(0, Math.min(start, viewportWidth - outerWidth));

  bubble.style.left = `${String(left)}px`;
  bubble.style.top = `${String(top)}px`;
  bubble.dataset.placement = placement;
}

/**
 * Attaches help text to a trigger, shown while the trigger is hovered or focused and
 * dismissible with Escape.
 *
 * Escape that dismisses an open tooltip stops there: a capture-phase listener on the document,
 * present only while the bubble is open, stops its propagation before any element's handler
 * runs, so a surrounding `Dialog` does not also close, even when the tooltip was opened by
 * hover and focus is in another field of the dialog. Escape while the tooltip is closed
 * propagates normally, so the next press closes the `Dialog`.
 *
 * Placement is viewport-aware. While open, the bubble is laid out with `position: fixed` from
 * the trigger's viewport rectangle: below the trigger, flipped above when there is not enough
 * room below and more above, and shifted and narrowed along the inline axis to stay inside the
 * viewport, in left-to-right and right-to-left text alike. It is placed before paint and again
 * after every render, on window resize and on scroll of any ancestor. A container that clips
 * or scrolls its overflow, such as a `DataTable` region or a `Dialog` panel, therefore never
 * clips it. The bubble is not portalled: it stays a DOM descendant of the wrapper, so it is
 * hoverable and the `aria-describedby` reference keeps resolving. An ancestor with
 * `transform`, `filter`, `perspective`, `contain` or a `will-change` of those would become
 * the bubble's containing block and offset it; no design-system wrapper sets them.
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
  // Whether an element child has no keyboard-reachable element carrying the tooltip's id, so
  // the trigger span must be the focus stop instead (see TooltipProps.children).
  const [needsFocusStop, setNeedsFocusStop] = useState(false);
  const triggerRef = useRef<HTMLSpanElement>(null);
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const bubbleRef = useRef<HTMLSpanElement>(null);
  const enabled = hasContent(content);
  const elementChild = isValidElement<DescribedProps>(children);

  // Empty content renders the children alone, without the wrapper and its handlers, so no
  // pointer leave or blur can reach this component until content returns. Hover, focus and
  // Escape state is therefore cleared as soon as content becomes empty (during this render,
  // before anything commits), so restored content starts closed instead of reopening from
  // stale state; the next hover or focus opens it as usual.
  if (!enabled && (hovered || focused || dismissed)) {
    setHovered(false);
    setFocused(false);
    setDismissed(false);
  }
  const open = enabled && (hovered || focused) && !dismissed;

  // Decides, from the committed and rendered DOM, whether an element child needs the focusable
  // span. It reads only the child's subtree and how it is rendered, never the span's own
  // attributes, so adding or removing the span's focus stop cannot flip the result back (no
  // oscillation). When the child becomes reachable while the span holds focus, focus moves to
  // the child before the span stops being focusable; the move stays inside the wrapper, so the
  // tooltip stays open.
  const reconcileTrigger = (): void => {
    const trigger = triggerRef.current;
    if (!elementChild || trigger === null) {
      return;
    }
    const tabStop = describedTabStop(trigger, tooltipId);
    if (tabStop !== null && document.activeElement === trigger) {
      tabStop.focus();
    }
    setNeedsFocusStop(tabStop === null);
  };

  // After every commit, before paint: a re-render may have disabled, enabled or replaced the
  // child. Setting the same value again is a no-op, so this never loops.
  useLayoutEffect(reconcileTrigger);

  // Changes that reach the trigger without this component re-rendering re-run the check too:
  // - the child changing its own DOM (a composite child's internal state, for example), caught
  //   by observing the trigger's subtree for the attributes in triggerAttributes;
  // - rendering changes that touch no attribute inside the trigger: a stylesheet rule or an
  //   ancestor's class or `hidden` attribute switching the child, or the whole tooltip, between
  //   `display: none` and rendered (a collapsed table row or a hidden tab panel being revealed).
  //   They resize the wrapper between an empty box and its content, so a ResizeObserver sees
  //   them where the engine has one (jsdom has none). The wrapper is observed rather than the
  //   trigger span, because that span is an inline box while it is not the focus stop, and a
  //   ResizeObserver reports a constant empty size for inline boxes. The bubble is laid out
  //   with `position: fixed`, so opening it never resizes the wrapper;
  // - a `details` or popover around the tooltip opening or closing, which can leave the
  //   wrapper's size unchanged and is not observed by the ResizeObserver while closed; its
  //   `toggle` event does not bubble, so it is captured on the document.
  // Focus or the pointer entering the wrapper re-runs the check as well (see onFocus and
  // onMouseEnter), as a last resort for a change none of these sees.
  useLayoutEffect(() => {
    const trigger = triggerRef.current;
    const wrapper = wrapperRef.current;
    if (!enabled || !elementChild || trigger === null || wrapper === null) {
      return undefined;
    }
    const mutations = new MutationObserver(reconcileTrigger);
    mutations.observe(trigger, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: triggerAttributes,
    });
    const resizes =
      typeof ResizeObserver === 'function' ? new ResizeObserver(reconcileTrigger) : null;
    resizes?.observe(wrapper);
    const onToggle = (event: Event): void => {
      if (isRelatedToggle(wrapper, event.target)) {
        reconcileTrigger();
      }
    };
    document.addEventListener('toggle', onToggle, true);
    return () => {
      mutations.disconnect();
      resizes?.disconnect();
      document.removeEventListener('toggle', onToggle, true);
    };
    // reconcileTrigger reads only refs, the stable id and a state setter, so the closure from
    // the render that created the observers stays correct for as long as they live.
  }, [enabled, elementChild]);

  // While open, Escape anywhere in the document dismisses the bubble (SC 1.4.13, dismissible),
  // whether it was opened by focus or by hover alone, when focus may sit in another field. The
  // listener runs in the capture phase on the document, so it sees the key before any handler
  // in the tree, an enclosing Dialog's included, and stops it there: that press only
  // dismisses the help. The listener exists only while the bubble is open, so once it is
  // closed Escape propagates normally and the next press reaches the Dialog.
  useEffect(() => {
    if (!open) {
      return;
    }
    const onDocumentKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        setDismissed(true);
      }
    };
    document.addEventListener('keydown', onDocumentKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onDocumentKeyDown, true);
    };
  }, [open]);

  // Placement (see placeBubble). Measuring and positioning in a layout effect happens after the
  // bubble's hidden attribute is removed and before the browser paints, so it never shows at a
  // stale position. A render while open may bring new content or a resized trigger, so the
  // bubble is placed again after every commit while open.
  const place = (): void => {
    const wrapper = wrapperRef.current;
    const bubble = bubbleRef.current;
    if (wrapper !== null && bubble !== null) {
      placeBubble(wrapper, bubble);
    }
  };
  useLayoutEffect(() => {
    if (open) {
      place();
    }
  });

  // The trigger's viewport rectangle also moves when the window resizes or when the page or
  // any scrolling ancestor scrolls; scroll does not bubble, so it is captured on the window.
  useLayoutEffect(() => {
    if (!open) {
      return undefined;
    }
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, { capture: true, passive: true });
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, { capture: true });
    };
    // place reads only refs, so the closure from the render that opened the bubble stays
    // correct until it closes.
  }, [open]);

  if (!enabled) {
    return <>{children}</>;
  }

  const onMouseEnter: MouseEventHandler<HTMLSpanElement> = () => {
    reconcileTrigger();
    setHovered(true);
    setDismissed(false);
  };

  const onMouseLeave: MouseEventHandler<HTMLSpanElement> = () => {
    setHovered(false);
  };

  // React's onFocus and onBlur bubble (focusin and focusout), so focus moving between two
  // elements inside the wrapper fires both; only focus crossing the wrapper's edge counts.
  // Focus entering re-runs the trigger check first: when a change no observer saw left the
  // span focusable although the child is reachable, focus moves on to the child and the span
  // stops being a focus stop, so Tab never visits two described stops.
  const onFocus: FocusEventHandler<HTMLSpanElement> = (event) => {
    if (isInside(event.currentTarget, event.relatedTarget)) {
      return;
    }
    reconcileTrigger();
    setFocused(true);
    setDismissed(false);
  };

  const onBlur: FocusEventHandler<HTMLSpanElement> = (event) => {
    if (isInside(event.currentTarget, event.relatedTarget)) {
      return;
    }
    setFocused(false);
  };

  // The trigger span is always rendered, so switching its focus stop on or off never remounts
  // the child. A string is never a valid element, so it always gets the focus stop; so would
  // any other non-element a JavaScript caller passes, which keeps every trigger reachable.
  const focusStop = !elementChild || needsFocusStop;
  const trigger = (
    <span
      ref={triggerRef}
      className={focusStop ? 'ds-tooltip__trigger' : undefined}
      tabIndex={focusStop ? 0 : undefined}
      aria-describedby={focusStop ? tooltipId : undefined}
    >
      {elementChild
        ? cloneElement(children, {
            'aria-describedby': mergeIdRefs(children.props['aria-describedby'], tooltipId),
          })
        : children}
    </span>
  );

  return (
    <span
      ref={wrapperRef}
      className="ds-tooltip"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onFocus={onFocus}
      onBlur={onBlur}
    >
      {trigger}
      <span
        ref={bubbleRef}
        className="ds-tooltip__content"
        role="tooltip"
        id={tooltipId}
        hidden={!open}
      >
        {content}
      </span>
    </span>
  );
}
