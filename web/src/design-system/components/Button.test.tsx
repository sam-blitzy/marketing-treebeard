/**
 * Contract tests for `Button`, the action button (AAP 0.4.2; `../GAPS.md`, binding procedure
 * step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: a native `<button>` named by its children,
 * `type="button"` unless asked otherwise, exactly one variant and one size modifier (defaults
 * `secondary` and `md`), native `disabled` for a disabled button that is neither loading nor
 * focused, `aria-disabled="true"` with the click cancelled for a loading button and for a
 * disabled one that keeps focus until it is left, `aria-busy="true"` and an `aria-hidden`
 * Spinner slot while loading with the accessible name unchanged, every other native attribute,
 * the focus handlers and `ref` forwarded, and the attributes it owns (`type`, `className`,
 * `disabled`, `aria-disabled`, `aria-busy`, `dangerouslySetInnerHTML`) never overridden by
 * forwarded props.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createRef } from 'react';
import type { FormEvent } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { Button } from './Button';
import type { ButtonProps, ButtonSize, ButtonVariant } from './Button';

const VARIANTS: readonly ButtonVariant[] = ['primary', 'secondary', 'quiet'];
const SIZES: readonly ButtonSize[] = ['sm', 'md'];

describe('Button', () => {
  it('renders a native button named by its children, with the default type and modifiers', () => {
    render(<Button>Mark done</Button>);

    const button = screen.getByRole('button', { name: 'Mark done' });
    expect(button.tagName).toBe('BUTTON');
    expect(button.getAttribute('type')).toBe('button');
    expect(button.className).toBe('ds-button ds-button--secondary ds-button--md');
    expect(button.querySelector('.ds-button__label')?.textContent).toBe('Mark done');
    expect(button.querySelector('.ds-button__spinner')).toBeNull();
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(button.hasAttribute('aria-busy')).toBe(false);
  });

  it.each(VARIANTS.flatMap((variant) => SIZES.map((size) => [variant, size] as const)))(
    'carries exactly the %s variant and %s size modifiers',
    (variant, size) => {
      render(
        <Button variant={variant} size={size}>
          Action
        </Button>,
      );

      const modifiers = Array.from(screen.getByRole('button').classList).filter((name) =>
        name.startsWith('ds-button--'),
      );
      expect(modifiers).toEqual([`ds-button--${variant}`, `ds-button--${size}`]);
    },
  );

  it('fires onClick when activated and is focusable', () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Sync now</Button>);

    const button = screen.getByRole('button', { name: 'Sync now' });
    act(() => {
      button.focus();
    });
    expect(document.activeElement).toBe(button);
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('never submits a surrounding form by default', () => {
    const onSubmit = vi.fn((event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
    });
    render(
      <form onSubmit={onSubmit}>
        <Button>Dismiss</Button>
      </form>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('submits its form when type is submit', () => {
    const onSubmit = vi.fn((event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
    });
    render(
      <form onSubmit={onSubmit}>
        <Button type="submit" variant="primary">
          Sign in
        </Button>
      </form>,
    );

    const button = screen.getByRole('button', { name: 'Sign in' });
    expect(button.getAttribute('type')).toBe('submit');
    fireEvent.click(button);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('is natively disabled when disabled from its first render, out of the tab order, and fires no click', () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Snooze
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Snooze' });
    expect(button.hasAttribute('disabled')).toBe(true);
    expect(button.hasAttribute('aria-disabled')).toBe(false);
    expect(button.hasAttribute('aria-busy')).toBe(false);
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
    act(() => {
      button.focus();
    });
    expect(document.activeElement).not.toBe(button);
  });

  it('while loading is aria-disabled and busy, keeps its name and hides the Spinner slot', () => {
    const onClick = vi.fn();
    render(
      <Button loading disabled={false} onClick={onClick}>
        Sync now
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Sync now' });
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(button.getAttribute('aria-busy')).toBe('true');

    const slot = button.querySelector('.ds-button__spinner');
    expect(slot?.getAttribute('aria-hidden')).toBe('true');
    expect(slot?.previousElementSibling?.className).toBe('ds-button__label');
    const spinner = button.querySelector('.ds-spinner');
    expect(spinner).not.toBeNull();
    expect(spinner?.closest('[aria-hidden="true"]')).toBe(slot);
    // The Spinner is rendered but sits outside the accessibility tree.
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('status', { hidden: true }).textContent).toBe('Loading');

    // fireEvent.click returns false when a handler cancelled the event.
    expect(fireEvent.click(button)).toBe(false);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('becomes enabled again when loading ends', () => {
    const { rerender } = render(<Button loading>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' }).getAttribute('aria-disabled')).toBe(
      'true',
    );

    rerender(<Button loading={false}>Save</Button>);

    const button = screen.getByRole('button', { name: 'Save' });
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(button.hasAttribute('aria-disabled')).toBe(false);
    expect(button.hasAttribute('aria-busy')).toBe(false);
    expect(button.querySelector('.ds-button__spinner')).toBeNull();
  });

  it('keeps keyboard focus when it starts loading while focused, and stays a tab stop', () => {
    const onClick = vi.fn();
    const { rerender } = render(<Button onClick={onClick}>Save decision</Button>);
    const button = screen.getByRole('button', { name: 'Save decision' });
    act(() => {
      button.focus();
    });

    rerender(
      <Button loading disabled onClick={onClick}>
        Save decision
      </Button>,
    );

    // Loading wins over disabled: the button is never natively disabled while it loads.
    expect(document.activeElement).toBe(button);
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(button.getAttribute('aria-busy')).toBe('true');
    expect(button.tabIndex).toBe(0);
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('neither submits its form nor runs onClick while loading, by pointer or implicit submission', () => {
    const onClick = vi.fn();
    const onSubmit = vi.fn((event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
    });
    render(
      <form onSubmit={onSubmit}>
        <input aria-label="Daily budget" />
        <Button type="submit" variant="primary" loading onClick={onClick}>
          Save decision
        </Button>
      </form>,
    );

    const button = screen.getByRole('button', { name: 'Save decision' });
    // A browser submits a form implicitly (Enter in a field) by firing a click at its default
    // button, the first submit button, so a pointer click and Enter in a field both reach the
    // button as this click, which the button cancels.
    const input = screen.getByRole('textbox', { name: 'Daily budget' });
    expect(input.closest('form')?.querySelector('button[type="submit"]')).toBe(button);
    expect(fireEvent.click(button)).toBe(false);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onClick).not.toHaveBeenCalled();
  });

  it('stays focusable as aria-disabled when disabled while focused, until focus leaves it', () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <>
        <Button onClick={onClick}>14 days</Button>
        <Button>Elsewhere</Button>
      </>,
    );
    const button = screen.getByRole('button', { name: '14 days' });
    act(() => {
      button.focus();
    });

    rerender(
      <>
        <Button disabled onClick={onClick}>
          14 days
        </Button>
        <Button>Elsewhere</Button>
      </>,
    );

    expect(document.activeElement).toBe(button);
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(button.hasAttribute('aria-busy')).toBe(false);
    expect(fireEvent.click(button)).toBe(false);
    expect(onClick).not.toHaveBeenCalled();

    act(() => {
      screen.getByRole('button', { name: 'Elsewhere' }).focus();
    });

    expect(button.hasAttribute('disabled')).toBe(true);
    expect(button.hasAttribute('aria-disabled')).toBe(false);
    act(() => {
      button.focus();
    });
    expect(document.activeElement).not.toBe(button);
  });

  it('forwards onFocus and onBlur while it tracks focus', () => {
    const onFocus = vi.fn();
    const onBlur = vi.fn();
    render(
      <Button onFocus={onFocus} onBlur={onBlur}>
        Mark done
      </Button>,
    );
    const button = screen.getByRole('button', { name: 'Mark done' });

    act(() => {
      button.focus();
    });
    expect(onFocus).toHaveBeenCalledTimes(1);
    act(() => {
      button.blur();
    });
    expect(onBlur).toHaveBeenCalledTimes(1);
  });

  it('renders boolean ARIA states as the strings "true" and "false"', () => {
    // Toggle callers (DateRangePicker presets) and disclosures pass booleans; both values must
    // reach the DOM, because aria-expanded="false" is a state, not an absent attribute.
    render(
      <Button aria-pressed={true} aria-expanded={false}>
        Custom range
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Custom range' });
    expect(button.getAttribute('aria-pressed')).toBe('true');
    expect(button.getAttribute('aria-expanded')).toBe('false');
  });

  it('forwards native attributes, ARIA states and ref', () => {
    const ref = createRef<HTMLButtonElement>();
    render(
      <Button
        ref={ref}
        id="preset-28"
        name="preset"
        value="28"
        size="sm"
        aria-pressed
        aria-label="28 days (default)"
      >
        28 days
      </Button>,
    );

    const button = screen.getByRole('button', { name: '28 days (default)', pressed: true });
    expect(ref.current).toBe(button);
    expect(button.id).toBe('preset-28');
    expect(button.getAttribute('name')).toBe('preset');
    expect(button.getAttribute('value')).toBe('28');
  });

  it('keeps the attributes it owns when untyped props try to override them', () => {
    // Props that reach the component without its types, as a spread of an untyped object would.
    const untyped = {
      className: 'injected',
      'aria-busy': false,
      'aria-disabled': false,
      dangerouslySetInnerHTML: { __html: '<em>raw</em>' },
    } as unknown as ButtonProps;
    render(
      <Button {...untyped} loading variant="quiet">
        Escaped label
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Escaped label' });
    expect(button.className).toBe('ds-button ds-button--quiet ds-button--md');
    expect(button.getAttribute('aria-busy')).toBe('true');
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(button.querySelector('em')).toBeNull();
    expect(button.querySelector('.ds-button__label')?.textContent).toBe('Escaped label');
  });
});
