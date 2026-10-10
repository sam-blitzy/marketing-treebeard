/**
 * Contract tests for `Tooltip`, the design-system help-text wrapper (AAP 0.4.2; `../GAPS.md`
 * row 11).
 *
 * The cases hold the wrapper to the contract in its TSDoc, which must survive the later Blitzy OS
 * binding: a `role="tooltip"` bubble that its trigger references through `aria-describedby` and
 * that stays in the DOM while hidden; the props `content` and `children`, including the empty
 * content rule and which trigger becomes the keyboard focus stop; WCAG 2.2 SC 1.4.13 (hover and
 * focus open it, it persists while either lasts, it is hoverable, and Escape dismisses it
 * without letting the press through); and the viewport-aware placement. jsdom computes no
 * layout, so placement cases give the wrapper and the bubble explicit rectangles, and a small
 * stylesheet stands in for the margin and maximum width `Tooltip.css` takes from the tokens.
 *
 * @packageDocumentation
 */
import { useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';

// Button is imported on purpose: the Tooltip contract names Button, including its disabled and
// loading states, as a trigger that forwards aria-describedby, so those cases need the real one.
import { Button } from './Button';
import { Tooltip } from './Tooltip';

/** The tooltip's parts, located through its `role="tooltip"` bubble. */
interface TooltipParts {
  /** The `span.ds-tooltip` wrapper that carries the hover and focus handlers. */
  wrapper: HTMLElement;
  /** The span that holds the child, and is the focus stop when the child has none. */
  trigger: HTMLElement;
  /** The `role="tooltip"` bubble. */
  bubble: HTMLElement;
}

/**
 * Finds the parts of the only tooltip on the page, whether or not it is open.
 *
 * @returns the wrapper, trigger span and bubble
 */
function parts(): TooltipParts {
  const bubble = screen.getByRole('tooltip', { hidden: true });
  const wrapper = bubble.parentElement;
  const trigger = wrapper?.firstElementChild;
  if (wrapper === null || !(trigger instanceof HTMLElement)) {
    throw new Error('Tooltip structure not found');
  }
  return { wrapper, trigger, bubble };
}

/**
 * Whether the trigger span is the tooltip's own focus stop.
 *
 * @param trigger - the trigger span
 * @param bubble - the bubble whose id the stop must reference
 * @returns `true` when the span is focusable, classed and describes the bubble
 */
function isFocusStop(trigger: HTMLElement, bubble: HTMLElement): boolean {
  return (
    trigger.getAttribute('tabindex') === '0' &&
    trigger.classList.contains('ds-tooltip__trigger') &&
    trigger.getAttribute('aria-describedby') === bubble.id
  );
}

/** Whether the bubble is shown, judged by the accessibility tree rather than the attribute. */
function isOpen(): boolean {
  return screen.queryByRole('tooltip') !== null;
}

/**
 * Focuses `element` inside `act`, so the focus handlers' state updates are applied.
 *
 * @param element - the element to focus
 */
function focus(element: HTMLElement): void {
  act(() => {
    element.focus();
  });
}

/** Moves focus to the document body inside `act`. */
function blurAll(): void {
  act(() => {
    if (document.activeElement instanceof HTMLElement) {
      document.activeElement.blur();
    }
  });
}

/** A composite child that drops `aria-describedby` instead of passing it to its button. */
function DropsDescription(props: { 'aria-describedby'?: string }) {
  return <button type="button" data-dropped={props['aria-describedby'] ?? ''}>Composite</button>;
}

describe('Tooltip structure and naming', () => {
  it('wraps a string child in a focusable trigger span that the hidden tooltip describes', () => {
    render(<Tooltip content="Spend divided by Qualified Leads.">Cost per Qualified Lead</Tooltip>);
    const { wrapper, trigger, bubble } = parts();

    expect(wrapper.tagName).toBe('SPAN');
    expect(wrapper.className).toBe('ds-tooltip');
    expect(trigger.textContent).toBe('Cost per Qualified Lead');
    expect(isFocusStop(trigger, bubble)).toBe(true);
    expect(bubble.id).not.toBe('');
    expect(bubble.className).toBe('ds-tooltip__content');
    expect(bubble.textContent).toBe('Spend divided by Qualified Leads.');
    // Closed: still in the DOM, so the reference resolves, but out of the accessibility tree.
    expect(bubble.hidden).toBe(true);
    expect(isOpen()).toBe(false);
    expect(document.getElementById(bubble.id)).toBe(bubble);
  });

  it('gives each tooltip its own id', () => {
    render(
      <>
        <Tooltip content="First help.">First</Tooltip>
        <Tooltip content="Second help.">Second</Tooltip>
      </>,
    );
    const ids = screen.getAllByRole('tooltip', { hidden: true }).map((bubble) => bubble.id);

    expect(ids).toHaveLength(2);
    expect(ids.every((id) => id !== '')).toBe(true);
    expect(new Set(ids).size).toBe(2);
  });

  it("appends its id to an element child's aria-describedby and adds no focus stop when the child is reachable", () => {
    render(
      <>
        <p id="hint">Existing hint.</p>
        <Tooltip content="Requests a sync of every dataset.">
          <button type="button" aria-describedby="hint">
            Sync now
          </button>
        </Tooltip>
      </>,
    );
    const { trigger, bubble } = parts();
    const button = screen.getByRole('button', { name: 'Sync now' });

    expect(button.getAttribute('aria-describedby')).toBe(`hint ${bubble.id}`);
    expect(trigger.hasAttribute('tabindex')).toBe(false);
    expect(trigger.hasAttribute('class')).toBe(false);
    expect(trigger.hasAttribute('aria-describedby')).toBe(false);
    expect(trigger.contains(button)).toBe(true);
  });

  it('describes a design-system Button directly, because Button forwards aria-describedby', () => {
    render(
      <Tooltip content="Requests a sync of every dataset.">
        <Button variant="secondary">Sync now</Button>
      </Tooltip>,
    );
    const { trigger, bubble } = parts();

    expect(screen.getByRole('button', { name: 'Sync now' }).getAttribute('aria-describedby')).toBe(
      bubble.id,
    );
    expect(trigger.hasAttribute('tabindex')).toBe(false);
  });

  it('describes a loading Button directly, because it stays focusable while loading', () => {
    render(
      <Tooltip content="Requests a sync of every dataset.">
        <Button loading>Sync now</Button>
      </Tooltip>,
    );
    const { trigger, bubble } = parts();

    const button = screen.getByRole('button', { name: 'Sync now' });
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(button.getAttribute('aria-describedby')).toBe(bubble.id);
    expect(isFocusStop(trigger, bubble)).toBe(false);
  });

  it.each<[string, ReactElement]>([
    ['a plain element', <span key="plain">Window</span>],
    ['a disabled Button', <Button key="disabled" disabled>Sync now</Button>],
    [
      'a button hidden with display none',
      <button key="none" type="button" style={{ display: 'none' }}>
        Hidden
      </button>,
    ],
    [
      'a button with the hidden attribute',
      <button key="hidden" type="button" hidden>
        Hidden
      </button>,
    ],
    [
      'a button inside an inert subtree',
      <div key="inert" inert>
        <button type="button">Inert</button>
      </div>,
    ],
    [
      'a composite that drops aria-describedby',
      <DropsDescription key="composite" />,
    ],
  ])('makes the trigger span the focus stop for %s', (_name, child) => {
    render(<Tooltip content="Help.">{child}</Tooltip>);
    const { trigger, bubble } = parts();

    expect(isFocusStop(trigger, bubble)).toBe(true);
  });

  it('makes the trigger span the focus stop when the described button is inside a closed details', () => {
    render(
      <Tooltip content="Help.">
        <details>
          <summary>More</summary>
          <button type="button">Inside</button>
        </details>
      </Tooltip>,
    );
    const { trigger, bubble } = parts();

    // The id goes on the details element, which is no tab stop, and its button is concealed.
    expect(isFocusStop(trigger, bubble)).toBe(true);
  });

  it.each<[string, ReactNode]>([
    ['the empty string', ''],
    ['null', null],
    ['undefined', undefined],
    ['false', false],
    ['true', true],
  ])('returns the children unchanged when content is %s', (_name, content) => {
    const { container } = render(
      <Tooltip content={content}>
        <button type="button">Sync now</button>
      </Tooltip>,
    );

    expect(screen.queryByRole('tooltip', { hidden: true })).toBeNull();
    expect(container.querySelector('.ds-tooltip')).toBeNull();
    expect(container.innerHTML).toBe('<button type="button">Sync now</button>');
  });

  it('returns a string child as plain text, with no focus stop, when content is empty', () => {
    const { container } = render(<Tooltip content="">Cost per Qualified Lead</Tooltip>);

    expect(container.innerHTML).toBe('Cost per Qualified Lead');
  });

  it('treats 0 as content', () => {
    render(<Tooltip content={0}>Count</Tooltip>);

    expect(parts().bubble.textContent).toBe('0');
  });

  it('starts closed when content returns after being empty', () => {
    const { rerender } = render(<Tooltip content="Help.">Metric</Tooltip>);
    focus(parts().trigger);
    expect(isOpen()).toBe(true);

    rerender(<Tooltip content={null}>Metric</Tooltip>);
    expect(screen.queryByRole('tooltip', { hidden: true })).toBeNull();

    rerender(<Tooltip content="Help.">Metric</Tooltip>);
    expect(parts().bubble.hidden).toBe(true);
    expect(isOpen()).toBe(false);

    fireEvent.mouseEnter(parts().wrapper);
    expect(isOpen()).toBe(true);
  });
});

describe('Tooltip opening, persistence and dismissal', () => {
  it('opens on hover and closes when the pointer leaves', () => {
    render(<Tooltip content="Help.">Metric</Tooltip>);
    const { wrapper } = parts();

    fireEvent.mouseEnter(wrapper);
    expect(screen.getByRole('tooltip').textContent).toBe('Help.');

    fireEvent.mouseLeave(wrapper);
    expect(isOpen()).toBe(false);
  });

  it('opens on focus and closes on blur', () => {
    render(<Tooltip content="Help.">Metric</Tooltip>);

    focus(parts().trigger);
    expect(isOpen()).toBe(true);

    blurAll();
    expect(isOpen()).toBe(false);
  });

  it('describes a raw button child, opens on its focus, and closes on Escape and on its blur', () => {
    render(
      <Tooltip content="Spend divided by conversions">
        <button type="button">Cost/conversion</button>
      </Tooltip>,
    );
    const button = screen.getByRole('button', { name: 'Cost/conversion' });
    const { trigger, bubble } = parts();

    // Closed but present, so the button's reference resolves before the first focus, and the
    // button itself is the one described stop.
    expect(isOpen()).toBe(false);
    expect(bubble.hidden).toBe(true);
    expect(button.getAttribute('aria-describedby')).toBe(bubble.id);
    expect(trigger.hasAttribute('tabindex')).toBe(false);

    focus(button);
    expect(screen.getByRole('tooltip').textContent).toBe('Spend divided by conversions');

    fireEvent.keyDown(button, { key: 'Escape' });
    expect(isOpen()).toBe(false);
    expect(document.activeElement).toBe(button);

    // Focus leaving and returning starts a new focus, which reopens the dismissed bubble.
    act(() => {
      button.blur();
    });
    focus(button);
    expect(isOpen()).toBe(true);

    act(() => {
      button.blur();
    });
    expect(isOpen()).toBe(false);
  });

  it('opens when the described child itself takes focus', () => {
    render(
      <Tooltip content="Help.">
        <Button>Sync now</Button>
      </Tooltip>,
    );

    focus(screen.getByRole('button', { name: 'Sync now' }));

    expect(isOpen()).toBe(true);
  });

  it('stays open while either hover or focus lasts', () => {
    render(<Tooltip content="Help.">Metric</Tooltip>);
    const { wrapper, trigger } = parts();

    focus(trigger);
    fireEvent.mouseEnter(wrapper);
    fireEvent.mouseLeave(wrapper);
    expect(isOpen()).toBe(true);

    fireEvent.mouseEnter(wrapper);
    blurAll();
    expect(isOpen()).toBe(true);

    fireEvent.mouseLeave(wrapper);
    expect(isOpen()).toBe(false);
  });

  it('stays open while the pointer moves from the trigger onto the bubble', () => {
    render(<Tooltip content="Help.">Metric</Tooltip>);
    const { wrapper, trigger, bubble } = parts();
    expect(wrapper.contains(bubble)).toBe(true);

    fireEvent.mouseEnter(wrapper);
    fireEvent.mouseOut(trigger, { relatedTarget: bubble });
    fireEvent.mouseOver(bubble, { relatedTarget: trigger });
    expect(isOpen()).toBe(true);

    fireEvent.mouseOut(bubble, { relatedTarget: document.body });
    expect(isOpen()).toBe(false);
  });

  it('ignores focus moving between elements inside the wrapper', () => {
    render(
      <>
        <button type="button">Elsewhere</button>
        <Tooltip content="Help.">
          <div>
            <button type="button">One</button>
            <button type="button">Two</button>
          </div>
        </Tooltip>
      </>,
    );
    const { trigger, bubble } = parts();
    // The id lands on the div, which is no tab stop, so the span is the described stop.
    expect(isFocusStop(trigger, bubble)).toBe(true);

    focus(screen.getByRole('button', { name: 'One' }));
    expect(isOpen()).toBe(true);
    focus(screen.getByRole('button', { name: 'Two' }));
    expect(isOpen()).toBe(true);
    focus(trigger);
    expect(isOpen()).toBe(true);

    focus(screen.getByRole('button', { name: 'Elsewhere' }));
    expect(isOpen()).toBe(false);
  });

  it('dismisses on Escape without moving focus, and consumes that press', () => {
    const onKeyDown = vi.fn();
    render(
      <div onKeyDown={onKeyDown}>
        <Tooltip content="Help.">Metric</Tooltip>
      </div>,
    );
    const { trigger } = parts();
    focus(trigger);
    expect(isOpen()).toBe(true);

    fireEvent.keyDown(trigger, { key: 'Escape' });

    expect(isOpen()).toBe(false);
    expect(document.activeElement).toBe(trigger);
    expect(onKeyDown).not.toHaveBeenCalled();

    // Closed, the next Escape propagates as usual.
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(onKeyDown).toHaveBeenCalledTimes(1);
  });

  it('dismisses a tooltip opened by hover alone on Escape pressed in another field', () => {
    const onKeyDown = vi.fn();
    render(
      <>
        <input aria-label="Note" onKeyDown={onKeyDown} />
        <Tooltip content="Help.">Metric</Tooltip>
      </>,
    );
    const note = screen.getByRole('textbox', { name: 'Note' });
    focus(note);
    fireEvent.mouseEnter(parts().wrapper);
    expect(isOpen()).toBe(true);

    fireEvent.keyDown(note, { key: 'Escape' });

    expect(isOpen()).toBe(false);
    expect(onKeyDown).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(note);
  });

  it('dismisses a hover-opened tooltip on Escape dispatched on the document itself', () => {
    render(<Tooltip content="Help.">Metric</Tooltip>);
    const { wrapper } = parts();
    fireEvent.mouseEnter(wrapper);
    expect(isOpen()).toBe(true);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(isOpen()).toBe(false);
    // The pointer is still over the wrapper: the dismissal holds until the next hover starts.
    expect(parts().bubble.hidden).toBe(true);
  });

  it('keeps the bubble open on keys other than Escape and lets them through', () => {
    const onKeyDown = vi.fn();
    render(
      <div onKeyDown={onKeyDown}>
        <Tooltip content="Help.">Metric</Tooltip>
      </div>,
    );
    const { trigger } = parts();
    focus(trigger);

    fireEvent.keyDown(trigger, { key: 'Enter' });

    expect(isOpen()).toBe(true);
    expect(onKeyDown).toHaveBeenCalledTimes(1);
  });

  it('reopens after a dismissal on the next hover or the next focus', () => {
    render(
      <>
        <button type="button">Elsewhere</button>
        <Tooltip content="Help.">Metric</Tooltip>
      </>,
    );
    const { wrapper, trigger } = parts();

    fireEvent.mouseEnter(wrapper);
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(isOpen()).toBe(false);
    fireEvent.mouseLeave(wrapper);
    fireEvent.mouseEnter(wrapper);
    expect(isOpen()).toBe(true);
    fireEvent.mouseLeave(wrapper);

    focus(trigger);
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(isOpen()).toBe(false);
    focus(screen.getByRole('button', { name: 'Elsewhere' }));
    focus(trigger);
    expect(isOpen()).toBe(true);
  });
});

describe('Tooltip trigger reconciliation', () => {
  it('adds and removes the focus stop when the child is disabled and enabled by a re-render', () => {
    const { rerender } = render(
      <Tooltip content="Help.">
        <Button>Sync now</Button>
      </Tooltip>,
    );
    const { trigger, bubble } = parts();
    expect(isFocusStop(trigger, bubble)).toBe(false);

    rerender(
      <Tooltip content="Help.">
        <Button disabled>Sync now</Button>
      </Tooltip>,
    );
    expect(isFocusStop(trigger, bubble)).toBe(true);
    // The same button element is kept: switching the stop never remounts the child.
    const button = screen.getByRole('button', { name: 'Sync now' });

    rerender(
      <Tooltip content="Help.">
        <Button>Sync now</Button>
      </Tooltip>,
    );
    expect(isFocusStop(trigger, bubble)).toBe(false);
    expect(trigger.hasAttribute('tabindex')).toBe(false);
    expect(screen.getByRole('button', { name: 'Sync now' })).toBe(button);
  });

  it('moves focus from the span to the child when the child becomes reachable, and stays open', () => {
    const { rerender } = render(
      <Tooltip content="Help.">
        <Button disabled>Sync now</Button>
      </Tooltip>,
    );
    const { trigger } = parts();
    focus(trigger);
    expect(isOpen()).toBe(true);

    rerender(
      <Tooltip content="Help.">
        <Button>Sync now</Button>
      </Tooltip>,
    );

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Sync now' }));
    expect(trigger.hasAttribute('tabindex')).toBe(false);
    expect(isOpen()).toBe(true);
  });

  it("re-checks the trigger when the child's own DOM changes without a re-render", async () => {
    render(
      <Tooltip content="Help.">
        <button type="button">Sync now</button>
      </Tooltip>,
    );
    const { trigger, bubble } = parts();
    const button = screen.getByRole('button', { name: 'Sync now' });
    expect(isFocusStop(trigger, bubble)).toBe(false);

    await act(async () => {
      button.setAttribute('disabled', '');
      await Promise.resolve();
    });
    expect(isFocusStop(trigger, bubble)).toBe(true);

    await act(async () => {
      button.removeAttribute('disabled');
      await Promise.resolve();
    });
    expect(isFocusStop(trigger, bubble)).toBe(false);

    await act(async () => {
      button.style.display = 'none';
      await Promise.resolve();
    });
    expect(isFocusStop(trigger, bubble)).toBe(true);
  });

  it('re-checks the trigger when a details element around it toggles', () => {
    render(
      <>
        <details id="other">
          <summary>Other</summary>
        </details>
        <details id="around">
          <summary>More</summary>
          <Tooltip content="Help.">
            <button type="button">Inside</button>
          </Tooltip>
        </details>
      </>,
    );
    const { trigger, bubble } = parts();
    const around = document.getElementById('around');
    const other = document.getElementById('other');
    if (around === null || other === null) {
      throw new Error('details not found');
    }
    expect(isFocusStop(trigger, bubble)).toBe(true);

    around.setAttribute('open', '');
    // An unrelated toggle does not re-run the check, so the stale stop remains.
    act(() => {
      other.dispatchEvent(new Event('toggle'));
    });
    expect(isFocusStop(trigger, bubble)).toBe(true);

    act(() => {
      around.dispatchEvent(new Event('toggle'));
    });
    expect(isFocusStop(trigger, bubble)).toBe(false);
  });

  it('re-checks the trigger when the pointer enters, for changes no observer sees', async () => {
    const style = document.createElement('style');
    style.textContent = '.collapsed button { display: none; }';
    document.head.append(style);
    try {
      render(
        <div data-testid="row">
          <Tooltip content="Help.">
            <button type="button">Sync now</button>
          </Tooltip>
        </div>,
      );
      const { wrapper, trigger, bubble } = parts();
      expect(isFocusStop(trigger, bubble)).toBe(false);

      // A class on an ancestor outside the trigger: no mutation inside it, and jsdom has no
      // ResizeObserver.
      screen.getByTestId('row').classList.add('collapsed');
      expect(isFocusStop(trigger, bubble)).toBe(false);

      // The span gaining its stop is itself a mutation the observer reports in a microtask,
      // so that is flushed inside act too.
      await act(async () => {
        fireEvent.mouseEnter(wrapper);
        await Promise.resolve();
      });
      expect(isFocusStop(trigger, bubble)).toBe(true);
    } finally {
      style.remove();
    }
  });

  it('re-checks the trigger when focus enters, moving focus on to a child that became reachable', () => {
    const style = document.createElement('style');
    style.textContent = '.collapsed button { display: none; }';
    document.head.append(style);
    try {
      render(
        <div data-testid="row" className="collapsed">
          <Tooltip content="Help.">
            <button type="button">Sync now</button>
          </Tooltip>
        </div>,
      );
      const { trigger, bubble } = parts();
      expect(isFocusStop(trigger, bubble)).toBe(true);

      screen.getByTestId('row').classList.remove('collapsed');
      expect(isFocusStop(trigger, bubble)).toBe(true);

      focus(trigger);

      expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Sync now' }));
      expect(isFocusStop(trigger, bubble)).toBe(false);
      expect(isOpen()).toBe(true);
    } finally {
      style.remove();
    }
  });

  it('re-checks the trigger when a stateful child disables its own button', async () => {
    /** A child whose own state disables its button, as a composite control might. */
    function SelfDisabling(props: { 'aria-describedby'?: string }) {
      const [disabled, setDisabled] = useState(false);
      return (
        <span>
          <button
            type="button"
            aria-describedby={props['aria-describedby']}
            disabled={disabled}
            onClick={() => setDisabled(true)}
          >
            Run once
          </button>
        </span>
      );
    }
    render(
      <Tooltip content="Help.">
        <SelfDisabling />
      </Tooltip>,
    );
    const { trigger, bubble } = parts();
    expect(isFocusStop(trigger, bubble)).toBe(false);

    await act(async () => {
      screen.getByRole('button', { name: 'Run once' }).click();
      await Promise.resolve();
    });

    expect(isFocusStop(trigger, bubble)).toBe(true);
  });
});

describe('Tooltip placement', () => {
  let tokens: HTMLStyleElement;

  beforeEach(() => {
    // Stands in for Tooltip.css: an 8px margin on every side and a 320px maximum width.
    tokens = document.createElement('style');
    tokens.textContent = '.ds-tooltip__content { margin: 8px; max-width: 320px; }';
    document.head.append(tokens);
  });

  afterEach(() => {
    tokens.remove();
  });

  /**
   * Gives `element` a fixed viewport rectangle.
   *
   * @param element - the wrapper or the bubble
   * @param x - left edge
   * @param y - top edge
   * @param width - width
   * @param height - height
   */
  function setRect(element: HTMLElement, x: number, y: number, width: number, height: number) {
    Object.defineProperty(element, 'getBoundingClientRect', {
      configurable: true,
      value: () => new DOMRect(x, y, width, height),
    });
  }

  /**
   * Renders a tooltip with the given trigger rectangle and a 200 by 40 bubble, then opens it.
   *
   * @returns its parts
   */
  function openAt(x: number, y: number, width = 100, height = 20): TooltipParts {
    render(<Tooltip content="Help.">Metric</Tooltip>);
    const found = parts();
    setRect(found.wrapper, x, y, width, height);
    setRect(found.bubble, 0, 0, 200, 40);
    fireEvent.mouseEnter(found.wrapper);
    return found;
  }

  it('opens below the trigger, aligned to its inline start, with the margins as gaps', () => {
    const { bubble } = openAt(50, 100);

    expect(bubble.dataset.placement).toBe('bottom');
    expect(bubble.style.top).toBe('120px');
    expect(bubble.style.left).toBe('42px');
    expect(bubble.style.maxWidth).toBe('');
  });

  it('flips above the trigger when there is not enough room below and more above', () => {
    // The jsdom viewport is 1024 by 768: 18px below, 730px above, 56px needed.
    const { bubble } = openAt(50, 730);

    expect(bubble.dataset.placement).toBe('top');
    expect(bubble.style.top).toBe('674px');
  });

  it('stays below when the room above is not larger than the room below', () => {
    const { bubble } = openAt(50, 10, 100, 740);

    expect(bubble.dataset.placement).toBe('bottom');
    expect(bubble.style.top).toBe('750px');
  });

  it('shifts along the inline axis to stay inside the viewport', () => {
    const { bubble } = openAt(1000, 100);

    // 1024 viewport less the 216px margin box.
    expect(bubble.style.left).toBe('808px');
  });

  it('aligns to the inline end in right-to-left text and never starts before the viewport', () => {
    tokens.textContent += ' .ds-tooltip__content { direction: rtl; }';
    const { bubble, wrapper } = openAt(400, 100);

    // Trigger right edge 500, less the 200px bubble and its 8px start margin.
    expect(bubble.style.left).toBe('292px');

    setRect(wrapper, 0, 100, 100, 20);
    fireEvent(window, new Event('resize'));
    expect(bubble.style.left).toBe('0px');
  });

  it('narrows the bubble to a viewport narrower than its maximum width, and widens it again', () => {
    const clientWidth = vi.spyOn(document.documentElement, 'clientWidth', 'get');
    const clientHeight = vi.spyOn(document.documentElement, 'clientHeight', 'get');
    clientWidth.mockReturnValue(300);
    clientHeight.mockReturnValue(600);
    try {
      const { bubble } = openAt(10, 100);
      // 300 less the 8px margins on both sides.
      expect(bubble.style.maxWidth).toBe('284px');

      clientWidth.mockReturnValue(1024);
      fireEvent(window, new Event('resize'));
      expect(bubble.style.maxWidth).toBe('');
    } finally {
      clientWidth.mockRestore();
      clientHeight.mockRestore();
    }
  });

  it('is placed again on resize, on scroll of any ancestor and after a render, but not once closed', () => {
    const { rerender } = render(
      <div data-testid="scroller">
        <Tooltip content="Help.">Metric</Tooltip>
      </div>,
    );
    const { wrapper, bubble } = parts();
    setRect(wrapper, 50, 100, 100, 20);
    setRect(bubble, 0, 0, 200, 40);
    fireEvent.mouseEnter(wrapper);
    expect(bubble.style.top).toBe('120px');

    setRect(wrapper, 50, 200, 100, 20);
    fireEvent(window, new Event('resize'));
    expect(bubble.style.top).toBe('220px');

    setRect(wrapper, 50, 300, 100, 20);
    fireEvent.scroll(screen.getByTestId('scroller'));
    expect(bubble.style.top).toBe('320px');

    setRect(wrapper, 60, 300, 100, 20);
    rerender(
      <div data-testid="scroller">
        <Tooltip content="Longer help.">Metric</Tooltip>
      </div>,
    );
    expect(bubble.style.left).toBe('52px');

    fireEvent.mouseLeave(wrapper);
    setRect(wrapper, 50, 400, 100, 20);
    fireEvent(window, new Event('resize'));
    fireEvent.scroll(screen.getByTestId('scroller'));
    expect(bubble.style.top).toBe('320px');
  });

  it('places the bubble at the top-left without error where no layout exists', () => {
    render(<Tooltip content="Help.">Metric</Tooltip>);
    const { wrapper, bubble } = parts();

    fireEvent.mouseEnter(wrapper);

    expect(bubble.style.top).toBe('0px');
    expect(bubble.style.left).toBe('0px');
    expect(bubble.dataset.placement).toBe('bottom');
  });
});
