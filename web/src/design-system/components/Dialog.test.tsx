/**
 * Contract tests for `Dialog`, the design-system modal wrapper (AAP 0.4.2; `../GAPS.md` row 10).
 *
 * The cases hold the wrapper to the contract in its TSDoc, which must survive the later Blitzy OS
 * binding: `role="dialog"` with `aria-modal`, named by its level-2 title; the props `open`,
 * `title`, `onClose`, `children`, `footer` and `closeLabel`; initial focus, the Tab and
 * Shift+Tab trap with its sentinels and document focus guard, Escape, the Close button and the
 * backdrop; and focus returning to the opener. jsdom performs no sequential navigation of its
 * own, so a Tab the dialog leaves to the browser shows as a keydown that is not prevented, and
 * the browser's step is simulated by focusing where the browser would go.
 *
 * The last block repeats the core contract with a `TextField` body and a footer action, the
 * shape `DecisionForm` gives the dialog on "Record decision", so the wrapper is also proven with
 * a real design-system field rather than a raw input alone.
 *
 * @packageDocumentation
 */
import { StrictMode, useState } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { act, fireEvent, render, screen, within } from '@testing-library/react';

import { Dialog } from './Dialog';
import { TextField } from './TextField';
import { Tooltip } from './Tooltip';

/** The dialog's panel, the `role="dialog"` element. */
function panel(): HTMLElement {
  return screen.getByRole('dialog');
}

/**
 * The dialog's two focus sentinels, before and after the panel.
 *
 * @returns the leading and trailing sentinels
 */
function sentinels(): { leading: HTMLElement; trailing: HTMLElement } {
  const root = panel().parentElement;
  const leading = root?.firstElementChild;
  const trailing = root?.lastElementChild;
  if (!(leading instanceof HTMLElement) || !(trailing instanceof HTMLElement)) {
    throw new Error('Dialog sentinels not found');
  }
  return { leading, trailing };
}

/**
 * Dispatches a Tab (or Shift+Tab) keydown on the focused element.
 *
 * @param shiftKey - `true` for Shift+Tab
 * @param modifiers - extra modifier keys held with Tab
 * @returns `true` when no handler prevented the default action, so the browser would move focus
 */
function pressTab(
  shiftKey = false,
  modifiers: { altKey?: boolean; ctrlKey?: boolean; metaKey?: boolean } = {},
): boolean {
  const target = document.activeElement ?? document.body;
  return fireEvent.keyDown(target, { key: 'Tab', code: 'Tab', shiftKey, ...modifiers });
}

/** The props a test passes to {@link Harness}. */
interface HarnessProps {
  /** Whether the dialog starts open. */
  initiallyOpen?: boolean;
  /** Spy called whenever the dialog asks to close, before the harness closes it. */
  onCloseSpy?: () => void;
  /** The dialog's content. */
  children?: ReactNode;
}

/**
 * A parent that owns `open`, as the usage contract requires: an opener button outside the
 * dialog opens it, and `onClose` closes it.
 */
function Harness({
  initiallyOpen = false,
  onCloseSpy,
  children = <input aria-label="Note" />,
}: HarnessProps) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open dialog
      </button>
      <button type="button">Outside</button>
      <Dialog
        open={open}
        title="Record decision"
        onClose={() => {
          onCloseSpy?.();
          setOpen(false);
        }}
      >
        {children}
      </Dialog>
    </>
  );
}

/**
 * Opens the harness dialog through its opener, which takes focus first as a click would.
 *
 * @returns the opener button
 */
function openThroughOpener(): HTMLElement {
  const opener = screen.getByRole('button', { name: 'Open dialog' });
  opener.focus();
  fireEvent.click(opener);
  return opener;
}

/** Renders a component that portals a button outside the panel, as a nested menu would. */
function PortalledButton() {
  return createPortal(<button type="button">Portalled</button>, document.body);
}

describe('Dialog rendering and naming', () => {
  it('renders nothing while closed', () => {
    render(
      <Dialog open={false} title="Record decision" onClose={() => undefined}>
        <p>Body</p>
      </Dialog>,
    );

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.body.querySelector('.ds-dialog')).toBeNull();
    expect(screen.queryByText('Body')).toBeNull();
  });

  it('is a modal dialog named by its level-2 title and portalled to document.body', () => {
    const { container } = render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <p>Body</p>
      </Dialog>,
    );

    const dialog = screen.getByRole('dialog', { name: 'Record decision' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.tabIndex).toBe(-1);

    const heading = screen.getByRole('heading', { level: 2, name: 'Record decision' });
    expect(dialog.getAttribute('aria-labelledby')).toBe(heading.id);
    expect(heading.id).not.toBe('');
    expect(dialog.contains(heading)).toBe(true);

    expect(container.contains(dialog)).toBe(false);
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    expect(dialog.parentElement?.className).toBe('ds-dialog');
    expect(screen.getByText('Body').closest('.ds-dialog__body')).not.toBeNull();
  });

  it('renders focusable, unlabelled sentinels around the panel and an aria-hidden backdrop', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <p>Body</p>
      </Dialog>,
    );

    const { leading, trailing } = sentinels();
    for (const sentinel of [leading, trailing]) {
      expect(sentinel.tabIndex).toBe(0);
      expect(sentinel.className).toBe('ds-visually-hidden');
      expect(sentinel.hasAttribute('aria-hidden')).toBe(false);
      expect(sentinel.hasAttribute('role')).toBe(false);
      expect(sentinel.textContent).toBe('');
    }
    const backdrop = leading.nextElementSibling;
    expect(backdrop?.className).toBe('ds-dialog__backdrop');
    expect(backdrop?.getAttribute('aria-hidden')).toBe('true');
    expect(backdrop?.nextElementSibling).toBe(panel());
  });

  it('labels the Close button "Close" by default and closeLabel otherwise', () => {
    const { rerender } = render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <p>Body</p>
      </Dialog>,
    );
    const close = screen.getByRole('button', { name: 'Close' });
    expect(close.getAttribute('type')).toBe('button');
    expect(close.closest('.ds-dialog__header')).not.toBeNull();

    rerender(
      <Dialog open title="Record decision" onClose={() => undefined} closeLabel="Cancel">
        <p>Body</p>
      </Dialog>,
    );
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDefined();
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull();
  });

  it('renders the footer row only when a footer is given and not null', () => {
    const { rerender } = render(
      <Dialog
        open
        title="Record decision"
        onClose={() => undefined}
        footer={<button type="submit">Save decision</button>}
      >
        <p>Body</p>
      </Dialog>,
    );
    const save = screen.getByRole('button', { name: 'Save decision' });
    expect(save.parentElement?.className).toBe('ds-dialog__footer');
    expect(save.parentElement?.parentElement).toBe(panel());

    rerender(
      <Dialog open title="Record decision" onClose={() => undefined} footer={null}>
        <p>Body</p>
      </Dialog>,
    );
    expect(document.body.querySelector('.ds-dialog__footer')).toBeNull();

    rerender(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <p>Body</p>
      </Dialog>,
    );
    expect(document.body.querySelector('.ds-dialog__footer')).toBeNull();
  });

  it('throws rather than open without an accessible name', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      for (const title of ['', '   ', 42 as unknown as string]) {
        expect(() =>
          render(
            <Dialog open title={title} onClose={() => undefined}>
              <p>Body</p>
            </Dialog>,
          ),
        ).toThrow(/`title` must contain visible text/);
      }
    } finally {
      consoleError.mockRestore();
    }
  });

  it('accepts a blank title while closed, because nothing is rendered', () => {
    expect(() =>
      render(
        <Dialog open={false} title="" onClose={() => undefined}>
          <p>Body</p>
        </Dialog>,
      ),
    ).not.toThrow();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('Dialog closing', () => {
  it('asks the parent to close from the Close button, the backdrop and Escape', () => {
    const onClose = vi.fn();
    render(
      <Dialog open title="Record decision" onClose={onClose}>
        <input aria-label="Note" />
      </Dialog>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);

    const backdrop = document.body.querySelector('.ds-dialog__backdrop');
    if (backdrop === null) {
      throw new Error('backdrop not found');
    }
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Note' }), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it('stays open while the parent keeps open true', () => {
    const onClose = vi.fn();
    render(
      <Dialog open title="Saving" onClose={onClose}>
        <input aria-label="Note" />
      </Dialog>,
    );

    fireEvent.keyDown(panel(), { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));

    expect(onClose).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('dialog', { name: 'Saving' })).toBeDefined();
  });

  it('closes when the parent sets open to false from onClose', () => {
    const onCloseSpy = vi.fn();
    render(<Harness onCloseSpy={onCloseSpy} />);
    openThroughOpener();
    expect(screen.getByRole('dialog', { name: 'Record decision' })).toBeDefined();

    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Note' }), { key: 'Escape' });

    expect(onCloseSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('stops Escape on the panel so handlers outside the dialog never see it', () => {
    const outsideKeyDown = vi.fn();
    const documentKeyDown = vi.fn();
    document.addEventListener('keydown', documentKeyDown);
    try {
      render(
        <div onKeyDown={outsideKeyDown}>
          <Dialog open title="Record decision" onClose={() => undefined}>
            <input aria-label="Note" />
          </Dialog>
        </div>,
      );

      fireEvent.keyDown(screen.getByRole('textbox', { name: 'Note' }), { key: 'Escape' });

      expect(outsideKeyDown).not.toHaveBeenCalled();
      expect(documentKeyDown).not.toHaveBeenCalled();

      fireEvent.keyDown(screen.getByRole('textbox', { name: 'Note' }), { key: 'Enter' });
      expect(outsideKeyDown).toHaveBeenCalledTimes(1);
    } finally {
      document.removeEventListener('keydown', documentKeyDown);
    }
  });

  it('lets an open Tooltip inside it consume the first Escape, and closes on the next', () => {
    const onClose = vi.fn();
    render(
      <Dialog open title="Record decision" onClose={onClose}>
        <input aria-label="Note" />
        <Tooltip content="Spend divided by Qualified Leads.">Cost per Qualified Lead</Tooltip>
      </Dialog>,
    );
    const tooltip = screen.getByRole('tooltip', { hidden: true });
    const wrapper = tooltip.parentElement;
    if (wrapper === null) {
      throw new Error('tooltip wrapper not found');
    }
    // Opened by hover alone, while focus stays in another field of the dialog.
    const note = screen.getByRole('textbox', { name: 'Note' });
    expect(document.activeElement).toBe(note);
    fireEvent.mouseEnter(wrapper);
    expect(tooltip.hidden).toBe(false);

    fireEvent.keyDown(note, { key: 'Escape' });
    expect(tooltip.hidden).toBe(true);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(note, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('Dialog initial focus and focus return', () => {
  it('focuses the first tab stop of the body on open, skipping hidden, disabled and inert content', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <input aria-label="Hidden" style={{ display: 'none' }} />
        <input aria-label="Disabled" disabled />
        <div inert>
          <input aria-label="Inert" />
        </div>
        <div tabIndex={-1}>not a stop</div>
        <input aria-label="Status" />
        <input aria-label="Note" />
      </Dialog>,
    );

    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Status' }));
  });

  it('focuses the panel itself when the body has no tab stop, although the Close button has one', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <p>Read-only explanation.</p>
      </Dialog>,
    );

    expect(document.activeElement).toBe(panel());
  });

  it('keeps focus on a descendant that takes it with autoFocus', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <input aria-label="Status" />
        <input aria-label="Note" autoFocus />
      </Dialog>,
    );

    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));
  });

  it('returns focus to the opener when it closes', () => {
    render(<Harness />);
    const opener = openThroughOpener();
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it('returns focus to the opener recorded before an autoFocus descendant took focus', () => {
    render(
      <Harness>
        <input aria-label="Note" autoFocus />
      </Harness>,
    );
    const opener = openThroughOpener();
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));

    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });

    expect(document.activeElement).toBe(opener);
  });

  it('returns focus to the element focused when it opened, also on unmount', () => {
    const opener = document.createElement('button');
    opener.textContent = 'Opener';
    document.body.append(opener);
    opener.focus();
    try {
      const { unmount } = render(
        <Dialog open title="Record decision" onClose={() => undefined}>
          <input aria-label="Note" />
        </Dialog>,
      );
      expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));

      unmount();

      expect(document.activeElement).toBe(opener);
    } finally {
      opener.remove();
    }
  });

  it('leaves focus alone on close when the opener has left the document', () => {
    const opener = document.createElement('button');
    document.body.append(opener);
    opener.focus();
    const { rerender } = render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <input aria-label="Note" />
      </Dialog>,
    );
    opener.remove();

    rerender(
      <Dialog open={false} title="Record decision" onClose={() => undefined}>
        <input aria-label="Note" />
      </Dialog>,
    );

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener.isConnected).toBe(false);
    expect(document.activeElement).toBe(document.body);
  });

  it('does not move focus back to the first field when an open dialog re-renders', () => {
    const { rerender } = render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <input aria-label="Status" />
        <input aria-label="Note" />
      </Dialog>,
    );
    const note = screen.getByRole('textbox', { name: 'Note' });
    note.focus();

    rerender(
      <Dialog open title="Record decision" onClose={() => undefined} footer={<p>Saved</p>}>
        <input aria-label="Status" />
        <input aria-label="Note" />
      </Dialog>,
    );

    expect(document.activeElement).toBe(note);
  });

  it('keeps initial focus, autoFocus and focus return under StrictMode', () => {
    render(
      <StrictMode>
        <Harness>
          <input aria-label="Note" autoFocus />
        </Harness>
      </StrictMode>,
    );
    const opener = openThroughOpener();

    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(document.activeElement).toBe(opener);
  });

  it("gives an autoFocus descendant its focus back after StrictMode's simulated remount", () => {
    const opener = document.createElement('button');
    opener.textContent = 'Opener';
    document.body.append(opener);
    opener.focus();
    try {
      // Mounted open, so StrictMode runs the focus effect, its cleanup (which returns focus to
      // the opener) and the effect again; the second run must not fall back to the first field.
      const { unmount } = render(
        <StrictMode>
          <Dialog open title="Record decision" onClose={() => undefined}>
            <input aria-label="Status" />
            <input aria-label="Note" autoFocus />
          </Dialog>
        </StrictMode>,
      );

      expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));

      unmount();
      expect(document.activeElement).toBe(opener);
    } finally {
      opener.remove();
    }
  });

  it('keeps the first field focused under StrictMode', () => {
    render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    );
    openThroughOpener();

    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));
  });
});

describe('Dialog keyboard focus trap', () => {
  /** Renders an open dialog with a field in its body and a submit button in its footer. */
  function renderForm(): { close: HTMLElement; note: HTMLElement; save: HTMLElement } {
    render(
      <Dialog
        open
        title="Record decision"
        onClose={() => undefined}
        footer={<button type="submit">Save decision</button>}
      >
        <input aria-label="Note" />
      </Dialog>,
    );
    return {
      close: screen.getByRole('button', { name: 'Close' }),
      note: screen.getByRole('textbox', { name: 'Note' }),
      save: screen.getByRole('button', { name: 'Save decision' }),
    };
  }

  it('wraps Tab from the last stop to the first, the Close button included', () => {
    const { close, save } = renderForm();
    save.focus();

    expect(pressTab()).toBe(false);
    expect(document.activeElement).toBe(close);
  });

  it('wraps Shift+Tab from the first stop to the last', () => {
    const { close, save } = renderForm();
    close.focus();

    expect(pressTab(true)).toBe(false);
    expect(document.activeElement).toBe(save);
  });

  it('moves Shift+Tab from the panel itself to the last stop', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <p>No field.</p>
      </Dialog>,
    );
    expect(document.activeElement).toBe(panel());

    expect(pressTab(true)).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }));
  });

  it('moves Tab from the panel itself to the first stop', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <p>No field.</p>
      </Dialog>,
    );

    expect(pressTab()).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }));
  });

  it('leaves Tab between two inner stops to the browser', () => {
    const { close, note } = renderForm();
    close.focus();

    expect(pressTab()).toBe(true);
    expect(document.activeElement).toBe(close);

    note.focus();
    expect(pressTab()).toBe(true);
    expect(pressTab(true)).toBe(true);
    expect(document.activeElement).toBe(note);
  });

  it('leaves Tab with Alt, Ctrl or Meta, and a Tab a descendant handled, alone', () => {
    const onKeyDown = vi.fn((event: { key: string; preventDefault: () => void }) => {
      if (event.key === 'Tab') {
        event.preventDefault();
      }
    });
    render(
      <Dialog
        open
        title="Record decision"
        onClose={() => undefined}
        footer={
          <button type="submit" onKeyDown={onKeyDown}>
            Save decision
          </button>
        }
      >
        <input aria-label="Note" />
      </Dialog>,
    );
    const save = screen.getByRole('button', { name: 'Save decision' });

    // The handled Tab: the descendant prevented it, so focus stays.
    save.focus();
    expect(pressTab()).toBe(false);
    expect(onKeyDown).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(save);

    // Modifier shortcuts on the first stop, where a plain Shift+Tab would wrap: never
    // sequential navigation.
    const close = screen.getByRole('button', { name: 'Close' });
    close.focus();
    for (const modifiers of [{ altKey: true }, { ctrlKey: true }, { metaKey: true }]) {
      expect(pressTab(true, modifiers)).toBe(true);
      expect(document.activeElement).toBe(close);
    }
    expect(pressTab(true)).toBe(false);
    expect(document.activeElement).toBe(save);
  });

  it('ignores keys other than Tab and Escape', () => {
    const onClose = vi.fn();
    render(
      <Dialog open title="Record decision" onClose={onClose}>
        <input aria-label="Note" />
      </Dialog>,
    );
    const note = screen.getByRole('textbox', { name: 'Note' });

    expect(fireEvent.keyDown(note, { key: 'Enter' })).toBe(true);
    expect(fireEvent.keyDown(note, { key: 'ArrowDown' })).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(note);
  });

  it('follows the panel order around a positive tabindex', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <input aria-label="Note" />
        <button type="button" tabIndex={1}>
          First by tabindex
        </button>
      </Dialog>,
    );
    const positive = screen.getByRole('button', { name: 'First by tabindex' });
    const close = screen.getByRole('button', { name: 'Close' });
    const note = screen.getByRole('textbox', { name: 'Note' });
    expect(document.activeElement).toBe(positive);

    expect(pressTab()).toBe(false);
    expect(document.activeElement).toBe(close);

    expect(pressTab(true)).toBe(false);
    expect(document.activeElement).toBe(positive);

    expect(pressTab(true)).toBe(false);
    expect(document.activeElement).toBe(note);
  });

  it('keeps focus on the panel when it has no tab stop at all', () => {
    // A stylesheet that leaves even the Close button unrendered.
    const style = document.createElement('style');
    style.textContent = '.ds-dialog__header button { display: none; }';
    document.head.append(style);
    try {
      render(
        <Harness initiallyOpen>
          <p>Read-only explanation.</p>
        </Harness>,
      );
      const dialog = panel();
      expect(document.activeElement).toBe(dialog);

      expect(pressTab()).toBe(false);
      expect(document.activeElement).toBe(dialog);
      expect(pressTab(true)).toBe(false);
      expect(document.activeElement).toBe(dialog);

      act(() => {
        screen.getByRole('button', { name: 'Outside' }).focus();
      });
      expect(document.activeElement).toBe(dialog);
    } finally {
      style.remove();
    }
  });

  it('sends focus that reaches the trailing sentinel to the first stop', () => {
    const { close } = renderForm();

    act(() => {
      sentinels().trailing.focus();
    });

    expect(document.activeElement).toBe(close);
  });

  it('sends focus that reaches the leading sentinel to the last stop', () => {
    const { save } = renderForm();

    act(() => {
      sentinels().leading.focus();
    });

    expect(document.activeElement).toBe(save);
  });

  it('returns focus that reaches the page behind the modal to the first stop', () => {
    render(<Harness initiallyOpen />);
    const outside = screen.getByRole('button', { name: 'Outside' });

    act(() => {
      outside.focus();
    });

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }));
  });

  it('stops guarding the page once it closes', () => {
    render(<Harness initiallyOpen />);
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    const outside = screen.getByRole('button', { name: 'Outside' });

    outside.focus();

    expect(document.activeElement).toBe(outside);
  });

  it('treats content a descendant portals outside the panel as part of the dialog', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <input aria-label="Note" />
        <PortalledButton />
      </Dialog>,
    );
    const portalled = screen.getByRole('button', { name: 'Portalled' });
    expect(panel().contains(portalled)).toBe(false);

    act(() => {
      portalled.focus();
    });
    expect(document.activeElement).toBe(portalled);

    expect(pressTab()).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }));

    act(() => {
      portalled.focus();
    });
    expect(pressTab(true)).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Note' }));
  });

  it('leaves Tab on a last stop with internal navigation to the browser and catches it at the trailing sentinel', () => {
    render(
      <Dialog open title="Record decision" onClose={() => undefined}>
        <input aria-label="Status" />
        <input aria-label="Effective date" type="date" />
      </Dialog>,
    );
    const date = screen.getByLabelText('Effective date');
    date.focus();

    // The browser visits the date fields itself, then leaves the input for the sentinel.
    expect(pressTab()).toBe(true);
    expect(document.activeElement).toBe(date);
    act(() => {
      sentinels().trailing.focus();
    });

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }));
  });

  it('catches Shift+Tab out of a first stop with internal navigation at the leading sentinel', () => {
    render(
      <Dialog
        open
        title="Record decision"
        onClose={() => undefined}
        footer={<button type="submit">Save decision</button>}
      >
        <input aria-label="Effective date" type="date" tabIndex={1} />
      </Dialog>,
    );
    // With tabindex 1 the date input is the panel's first stop, ahead of the Close button.
    const date = screen.getByLabelText('Effective date');
    expect(document.activeElement).toBe(date);

    // Script cannot see which of its fields has focus, so Shift+Tab is left to the browser...
    expect(pressTab(true)).toBe(true);
    expect(document.activeElement).toBe(date);

    // ...which leaves its first field for the leading sentinel, sent on to the last stop.
    act(() => {
      sentinels().leading.focus();
    });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Save decision' }));
  });

  describe('Shift+Tab onto a last stop with internal navigation', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      // Spies on window.setTimeout and clearTimeout go first, then the real timers return.
      vi.restoreAllMocks();
      vi.useRealTimers();
    });

    /** Renders a dialog whose last stop is a date input, with the Close button focused. */
    function renderDateLast(): { close: HTMLElement; date: HTMLElement } {
      const result = render(
        <Dialog open title="Record decision" onClose={() => undefined}>
          <input aria-label="Effective date" type="date" />
        </Dialog>,
      );
      const close = screen.getByRole('button', { name: 'Close' });
      const date = result.getByLabelText('Effective date');
      close.focus();
      return { close, date };
    }

    it('hands the step to the browser from the trailing sentinel and keeps it there meanwhile', () => {
      const { date } = renderDateLast();

      expect(pressTab(true)).toBe(true);
      const { trailing } = sentinels();
      expect(document.activeElement).toBe(trailing);

      // The browser takes the step: it enters the date input at its last field. The fallback
      // then finds focus off the sentinel and leaves it there, so the field keeps its part.
      date.focus();
      const refocus = vi.spyOn(date, 'focus');
      act(() => {
        vi.runAllTimers();
      });
      expect(document.activeElement).toBe(date);
      expect(refocus).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    });

    it('moves focus to the last stop itself when the browser does not take the step', () => {
      const { date } = renderDateLast();

      expect(pressTab(true)).toBe(true);
      expect(document.activeElement).toBe(sentinels().trailing);

      act(() => {
        vi.runAllTimers();
      });
      expect(document.activeElement).toBe(date);
    });

    it('restarts the hand-off on a repeated Shift+Tab, with one fallback', () => {
      const { close, date } = renderDateLast();
      const setTimer = vi.spyOn(window, 'setTimeout');
      const clearTimer = vi.spyOn(window, 'clearTimeout');

      expect(pressTab(true)).toBe(true);
      expect(setTimer).toHaveBeenCalledTimes(1);
      const first = setTimer.mock.results[0]?.value as unknown;
      close.focus();
      expect(pressTab(true)).toBe(true);

      // The first fallback is cancelled before the second is scheduled.
      expect(setTimer).toHaveBeenCalledTimes(2);
      expect(clearTimer).toHaveBeenCalledWith(first);
      expect(document.activeElement).toBe(sentinels().trailing);

      act(() => {
        vi.runAllTimers();
      });
      expect(document.activeElement).toBe(date);
    });

    it('cancels a pending hand-off when it closes', () => {
      const opener = document.createElement('button');
      document.body.append(opener);
      opener.focus();
      try {
        const { rerender } = render(
          <Dialog open title="Record decision" onClose={() => undefined}>
            <input aria-label="Effective date" type="date" />
          </Dialog>,
        );
        screen.getByRole('button', { name: 'Close' }).focus();
        const setTimer = vi.spyOn(window, 'setTimeout');
        const clearTimer = vi.spyOn(window, 'clearTimeout');
        expect(pressTab(true)).toBe(true);
        expect(setTimer).toHaveBeenCalledTimes(1);
        const pending = setTimer.mock.results[0]?.value as unknown;

        rerender(
          <Dialog open={false} title="Record decision" onClose={() => undefined}>
            <input aria-label="Effective date" type="date" />
          </Dialog>,
        );

        expect(clearTimer).toHaveBeenCalledWith(pending);
        expect(document.activeElement).toBe(opener);
        act(() => {
          vi.runAllTimers();
        });
        expect(document.activeElement).toBe(opener);
      } finally {
        opener.remove();
      }
    });
  });
});

describe('Dialog with a TextField body (the DecisionForm case)', () => {
  /**
   * A parent shaped like "Record decision": an opener outside the dialog, a `TextField` as the
   * body's only tab stop and a submit action in the footer. The parent owns `open`, and
   * `onClose` closes the dialog, as the usage contract requires.
   */
  function DecisionFormHarness() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Open
        </button>
        <Dialog
          open={open}
          title="Record decision"
          onClose={() => setOpen(false)}
          footer={<button type="button">Save</button>}
        >
          <TextField label="Note" value="" onChange={() => undefined} />
        </Dialog>
      </>
    );
  }

  /**
   * Renders the harness and opens the dialog as a user does: the opener takes focus first, as a
   * click gives it in a browser (a jsdom click does not), and is then clicked.
   *
   * @returns the opener and the open dialog's panel
   */
  function openDecisionDialog(): { opener: HTMLElement; dialog: HTMLElement } {
    render(<DecisionFormHarness />);
    const opener = screen.getByRole('button', { name: 'Open' });
    act(() => {
      opener.focus();
    });
    fireEvent.click(opener);
    return { opener, dialog: screen.getByRole('dialog', { name: 'Record decision' }) };
  }

  /**
   * The open dialog's backdrop.
   *
   * @returns the `.ds-dialog__backdrop` element
   */
  function backdrop(): Element {
    const element = document.querySelector('.ds-dialog__backdrop');
    if (element === null) {
      throw new Error('Dialog backdrop not found');
    }
    return element;
  }

  it('renders no dialog until the opener is used', () => {
    render(<DecisionFormHarness />);

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByLabelText('Note')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });

  it('opens as a modal dialog named by its level-2 title, holding the field and the footer action', () => {
    const { dialog } = openDecisionDialog();

    expect(dialog.getAttribute('aria-modal')).toBe('true');
    const heading = within(dialog).getByRole('heading', { level: 2 });
    expect(heading.textContent).toBe('Record decision');
    expect(dialog.getAttribute('aria-labelledby')).toBe(heading.id);
    expect(dialog.contains(screen.getByLabelText('Note'))).toBe(true);
    const save = within(dialog).getByRole('button', { name: 'Save' });
    expect(save.closest('.ds-dialog__footer')).not.toBeNull();
  });

  it('focuses the TextField control, the first tab stop of the body, on open', () => {
    openDecisionDialog();

    const note = screen.getByLabelText('Note');
    expect(note.tagName).toBe('INPUT');
    expect(document.activeElement).toBe(note);
  });

  it('wraps Tab from the footer action to the Close button and Shift+Tab back, leaving inner steps to the browser', () => {
    const { dialog } = openDecisionDialog();
    const close = within(dialog).getByRole('button', { name: 'Close' });
    const note = screen.getByLabelText('Note');
    const save = within(dialog).getByRole('button', { name: 'Save' });

    // From the field to the footer action the browser's own step stays inside the panel.
    expect(fireEvent.keyDown(note, { key: 'Tab' })).toBe(true);
    expect(document.activeElement).toBe(note);

    act(() => {
      save.focus();
    });
    expect(fireEvent.keyDown(save, { key: 'Tab' })).toBe(false);
    expect(document.activeElement).toBe(close);

    expect(fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(save);
  });

  it('closes on Escape and returns focus to the opener', () => {
    const { opener, dialog } = openDecisionDialog();

    fireEvent.keyDown(dialog, { key: 'Escape' });

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByLabelText('Note')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it('closes from the Close button and returns focus to the opener', () => {
    const { opener, dialog } = openDecisionDialog();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it('closes from a backdrop click and returns focus to the opener, also after reopening', () => {
    const { opener } = openDecisionDialog();

    fireEvent.click(backdrop());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);

    // The opener holds focus again, so a second opening records it as the return target too.
    fireEvent.click(opener);
    expect(document.activeElement).toBe(screen.getByLabelText('Note'));
    fireEvent.click(backdrop());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it('calls a mock onClose once per Escape and stays open while the parent keeps open true', () => {
    const onClose = vi.fn();
    render(
      <Dialog
        open
        title="Record decision"
        onClose={onClose}
        footer={<button type="button">Save</button>}
      >
        <TextField label="Note" value="" onChange={() => undefined} />
      </Dialog>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Record decision' });
    const note = screen.getByLabelText('Note');

    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog', { name: 'Record decision' })).toBe(dialog);
    expect(document.activeElement).toBe(note);

    // Escape typed in the field, where focus sits while a decision is entered, reaches the panel.
    fireEvent.keyDown(note, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
