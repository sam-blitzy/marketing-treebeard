/**
 * Contract tests for `Badge`, the status and flag chip (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: one `<span>` carrying `ds-badge` and exactly
 * one tone modifier (default `neutral`), the label as its only content, no role or live region,
 * and an optional `id` another element can reference through `aria-describedby`.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { TONES } from '../tokens';
import { Badge } from './Badge';

describe('Badge', () => {
  it('renders its label in a span with the neutral tone by default', () => {
    render(<Badge>Shared budget</Badge>);

    const chip = screen.getByText('Shared budget');
    expect(chip.tagName).toBe('SPAN');
    expect(chip.className).toBe('ds-badge ds-badge--neutral');
  });

  it.each(TONES)('carries exactly one modifier for the %s tone', (tone) => {
    render(<Badge tone={tone}>Label</Badge>);

    const chip = screen.getByText('Label');
    const toneModifiers = Array.from(chip.classList).filter((name) =>
      name.startsWith('ds-badge--'),
    );
    expect(chip.classList.contains('ds-badge')).toBe(true);
    expect(toneModifiers).toEqual([`ds-badge--${tone}`]);
  });

  it('is static text: no role and no live region', () => {
    render(<Badge tone="warning">Stale</Badge>);

    const chip = screen.getByText('Stale');
    expect(chip.getAttribute('role')).toBeNull();
    expect(chip.getAttribute('aria-live')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('forwards its id, so another element can be described by the chip', () => {
    render(
      <>
        <button type="button" aria-describedby="flag-simulated">
          Record decision
        </button>
        <Badge id="flag-simulated" tone="info">
          Simulated data
        </Badge>
      </>,
    );

    expect(screen.getByText('Simulated data').id).toBe('flag-simulated');
    expect(
      screen.getByRole('button', { name: 'Record decision', description: 'Simulated data' }),
    ).toBeDefined();
  });

  it('renders no id attribute when none is given', () => {
    render(<Badge>Floor</Badge>);

    expect(screen.getByText('Floor').hasAttribute('id')).toBe(false);
  });
});
