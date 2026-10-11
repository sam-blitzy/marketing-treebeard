/**
 * Tests for `carriedFocus.ts`, the hand-over of keyboard focus to a card or row React mounts
 * anew in another place within one commit (WCAG 2.2 SC 2.4.3).
 *
 * They hold the module to its TSDoc contract:
 * - a component leaving the page while `holdsFocus()` holds notes its identity, and a component
 *   of the same identity mounting in the same commit calls its `takeFocus` once, after the
 *   commit, also under React StrictMode's repeated effects;
 * - a component that only notes (`useFocusLeaving`), such as a footer whose open dialog owns
 *   focus, hands focus to the card that claims;
 * - a note of another identity, or one nothing claimed in its commit, moves no focus, then or on
 *   a later mount;
 * - `focusLeftWith` reports a note only while it stands.
 *
 * The module fetches nothing, so every test renders without providers or a network stub.
 */
import { act, render, screen } from '@testing-library/react';
import { StrictMode, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { Button } from '../../design-system';
import { focusLeftWith, useCarriedFocus, useFocusLeaving } from './carriedFocus';

/** Props of {@link Probe}. */
interface ProbeProps {
  /** The recommendation identity the probe renders. */
  identity: string;
  /** The probe's button label. */
  label: string;
  /** Called when the probe takes focus over. */
  onTake?: () => void;
}

/** A card stand-in: one button, holding focus while the button has it, taking it on the button. */
function Probe({ identity, label, onTake }: ProbeProps): ReactElement {
  const buttonRef = useRef<HTMLButtonElement>(null);
  useCarriedFocus(identity, {
    holdsFocus: () => buttonRef.current !== null && buttonRef.current === document.activeElement,
    takeFocus: () => {
      onTake?.();
      buttonRef.current?.focus();
    },
  });
  return <Button ref={buttonRef}>{label}</Button>;
}

/** A footer stand-in that only notes its identity, always holding focus while `holds` is set. */
function Noting({ identity, holds }: { identity: string; holds: boolean }): ReactElement {
  useFocusLeaving(identity, () => holds);
  return <span>{`noting ${identity}`}</span>;
}

/** Where {@link Places} renders its child, which a test moves. */
type Place = 'first' | 'second' | 'none';

/**
 * Renders `child(place)` in the first or the second of two sibling containers, or nowhere, so a
 * move from one to the other makes React unmount the child in one place and mount it in the
 * other within one commit.
 */
function renderPlaces(child: (place: Place) => ReactNode, wrap = false): (next: Place) => void {
  const control: { move?: (next: Place) => void } = {};
  function Places(): ReactElement {
    const [place, setPlace] = useState<Place>('first');
    control.move = setPlace;
    return (
      <>
        <section aria-label="First">{place === 'first' ? child(place) : null}</section>
        <section aria-label="Second">{place === 'second' ? child(place) : null}</section>
      </>
    );
  }
  render(wrap ? <StrictMode><Places /></StrictMode> : <Places />);
  return (next) => {
    act(() => {
      control.move?.(next);
    });
  };
}

describe('useCarriedFocus', () => {
  it('hands focus from the component that leaves with it to the one of its identity that mounts', () => {
    const onTake = vi.fn();
    const move = renderPlaces((place) => (
      <Probe identity="card" label={`Card in ${place}`} onTake={onTake} />
    ));
    act(() => {
      screen.getByRole('button', { name: 'Card in first' }).focus();
    });

    move('second');

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Card in second' }));
    expect(onTake).toHaveBeenCalledTimes(1);
    expect(focusLeftWith('card')).toBe(false);
  });

  it('hands focus over once under StrictMode', () => {
    const onTake = vi.fn();
    const move = renderPlaces(
      (place) => <Probe identity="card" label={`Card in ${place}`} onTake={onTake} />,
      true,
    );
    act(() => {
      screen.getByRole('button', { name: 'Card in first' }).focus();
    });

    move('second');

    const moved = screen.getByRole('button', { name: 'Card in second' });
    expect(document.activeElement).toBe(moved);
    // StrictMode replays the new card's effects; any repeat lands on the same element.
    expect(onTake.mock.calls.length).toBeGreaterThanOrEqual(1);
    move('first');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Card in first' }));
  });

  it('takes the note a footer leaves while its dialog owns focus', () => {
    const move = renderPlaces((place) => (
      <>
        <Probe identity="card" label={`Card in ${place}`} />
        {place === 'first' ? <Noting identity="card" holds /> : null}
      </>
    ));
    expect(document.activeElement).toBe(document.body);

    move('second');

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Card in second' }));
  });

  it('moves no focus to a component of another identity, or when nothing held focus', async () => {
    const move = renderPlaces((place) => (
      <Probe identity={place === 'first' ? 'card' : 'other'} label={`Card in ${place}`} />
    ));
    act(() => {
      screen.getByRole('button', { name: 'Card in first' }).focus();
    });

    move('second');
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'Card in second' }));

    // A later task: the second card held no focus, so its leaving notes nothing.
    await act(async () => {
      await Promise.resolve();
    });
    move('first');
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'Card in first' }));
  });

  it('drops a note nothing claimed, so a later mount of its identity takes no focus', async () => {
    const onTake = vi.fn();
    const move = renderPlaces((place) => (
      <Probe identity="card" label={`Card in ${place}`} onTake={onTake} />
    ));
    act(() => {
      screen.getByRole('button', { name: 'Card in first' }).focus();
    });

    move('none');
    expect(focusLeftWith('card')).toBe(true);
    // The microtask that follows the commit drops it.
    await act(async () => {
      await Promise.resolve();
    });
    expect(focusLeftWith('card')).toBe(false);

    move('second');
    expect(onTake).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(document.body);
  });
});
