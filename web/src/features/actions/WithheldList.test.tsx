/**
 * Tests for `WithheldList`, the patterns a section withheld for sample size or validity
 * (AAP 0.5.4.2 P6: "patterns below their minimums are listed under `withheld` with observed and
 * required sizes"; AAP 0.1.2: "Withheld patterns still show their sample sizes"; AAP 0.5.5:
 * Budget changes lists items, then "Patterns withheld for sample size").
 *
 * The suite holds the module to its TSDoc contract:
 *
 * - {@link withheldLine} renders "{rule_id} · {entity}: {reason} — {observed} / {required}
 *   {unit}" from the API's own values: the entity's name, else its type and id, else
 *   "Account-wide"; the reason verbatim, dropped when blank; the sample through `formatSample`
 *   (grouped counts, fractions kept, a missing size "unavailable"), dropped when neither size is
 *   recorded. No threshold or minimum is written into the client.
 * - `WithheldList` renders its title (the given one, else "Withheld for sample size") above a
 *   list it names, one line per pattern in API order, and nothing at all when nothing is
 *   withheld.
 *
 * Withheld patterns come from `makeWithheld` (K1: 30 of 37 clicks) or are built in the test in
 * the backend's shape (`schemas.Withheld`). The component makes no request, so it renders
 * without providers.
 */
import { render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { EntityView, Withheld } from '../../api/types';
import { makeWithheld } from '../../test/fixtures';
import { WithheldList, withheldLine } from './WithheldList';

/** The default title, when the caller passes none or a blank one. */
const DEFAULT_TITLE = 'Withheld for sample size';

/** The title the Budget section passes (AAP 0.5.5). */
const BUDGET_TITLE = 'Patterns withheld for sample size';

/** An entity reference as the API serialises it: every field present, `null` when unknown. */
function entity(overrides: Partial<EntityView> = {}): EntityView {
  return { source: 'google', type: 'campaign', id: '812', name: 'Brand search', ...overrides };
}

/** A B4 weekday/weekend pattern withheld for too few weekend days (AAP 0.5.4.5). */
function b4Withheld(overrides: Partial<Withheld> = {}): Withheld {
  return makeWithheld({
    rule_id: 'B4',
    entity: entity(),
    reason: '3 weekend days of 4 required',
    sample: { observed: 3, required: 4, unit: 'weekend days' },
    ...overrides,
  });
}

/** The text of each line of the list named `name`. */
function lineTexts(name: string): string[] {
  return within(screen.getByRole('list', { name }))
    .getAllByRole('listitem')
    .map((line) => line.textContent);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('withheldLine', () => {
  it('states the rule, entity, reason and the observed against the required sample', () => {
    expect(withheldLine(b4Withheld())).toBe(
      'B4 · Brand search: 3 weekend days of 4 required — 3 / 4 weekend days',
    );
    expect(withheldLine(makeWithheld())).toBe(
      'K1 · crm pricing comparison: clicks 30 of 37 required — 30 / 37 clicks',
    );
  });

  it('formats sizes as counts: grouped, fractions kept, a missing size unavailable', () => {
    expect(
      withheldLine(b4Withheld({ sample: { observed: 1234, required: 3000, unit: 'clicks' } })),
    ).toBe('B4 · Brand search: 3 weekend days of 4 required — 1,234 / 3,000 clicks');
    expect(
      withheldLine(
        b4Withheld({
          reason: '2.9 expected outcomes of 3 required',
          sample: { observed: '2.900000', required: 3, unit: 'expected outcomes' },
        }),
      ),
    ).toBe('B4 · Brand search: 2.9 expected outcomes of 3 required — 2.9 / 3 expected outcomes');
    expect(
      withheldLine(b4Withheld({ sample: { observed: 9, required: null, unit: 'conversions' } })),
    ).toBe('B4 · Brand search: 3 weekend days of 4 required — 9 / unavailable conversions');
  });

  it('leaves the unit out when none is recorded', () => {
    expect(withheldLine(b4Withheld({ sample: { observed: 3, required: 4, unit: null } }))).toBe(
      'B4 · Brand search: 3 weekend days of 4 required — 3 / 4',
    );
  });

  it('ends at the reason when no sample applies, never showing "unavailable / unavailable"', () => {
    const line = withheldLine(
      makeWithheld({
        rule_id: 'C3a',
        entity: null,
        reason: 'Segment window not yet available',
        sample: { observed: null, required: null, unit: null },
      }),
    );

    expect(line).toBe('C3a · Account-wide: Segment window not yet available');
  });

  it('drops the reason clause when the reason is blank', () => {
    expect(withheldLine(b4Withheld({ reason: '   ' }))).toBe(
      'B4 · Brand search — 3 / 4 weekend days',
    );
  });

  it('trims the rule id and the reason', () => {
    expect(withheldLine(b4Withheld({ rule_id: ' B4 ', reason: '  no exposure  ' }))).toBe(
      'B4 · Brand search: no exposure — 3 / 4 weekend days',
    );
  });

  it.each<[string, EntityView | null, string]>([
    ['its name', entity({ name: '  Brand search  ' }), 'Brand search'],
    ['its type and id when it has no name', entity({ name: null }), 'campaign 812'],
    ['its id alone when it has no name or type', entity({ name: ' ', type: null }), '812'],
    ['its type alone when it has no name or id', entity({ name: null, id: null }), 'campaign'],
    [
      '"Account-wide" when it carries nothing',
      entity({ name: null, type: '', id: null }),
      'Account-wide',
    ],
    ['"Account-wide" for an account-wide withholding', null, 'Account-wide'],
  ])('names the entity by %s', (_label, value, expected) => {
    expect(withheldLine(b4Withheld({ entity: value }))).toBe(
      `B4 · ${expected}: 3 weekend days of 4 required — 3 / 4 weekend days`,
    );
  });

  it('accepts an entity recorded as plain text, and reads a blank one as account-wide', () => {
    // The contract types `entity` as an EntityView or null; the component also accepts a label
    // recorded as text, which only a cast can express here.
    const asText = (text: string) => text as unknown as Withheld['entity'];

    expect(withheldLine(b4Withheld({ entity: asText('Display Shared Budget') }))).toBe(
      'B4 · Display Shared Budget: 3 weekend days of 4 required — 3 / 4 weekend days',
    );
    expect(withheldLine(b4Withheld({ entity: asText('  ') }))).toBe(
      'B4 · Account-wide: 3 weekend days of 4 required — 3 / 4 weekend days',
    );
  });

  it('never prints "null" or "undefined"', () => {
    const line = withheldLine(
      makeWithheld({
        entity: { source: null, type: null, id: null, name: null },
        sample: { observed: null, required: 7, unit: null },
      }),
    );

    expect(line).toBe('K1 · Account-wide: clicks 30 of 37 required — unavailable / 7');
    expect(line).not.toMatch(/null|undefined/);
  });
});

describe('WithheldList', () => {
  it('lists each withheld pattern under the default title, in API order', () => {
    const withheld = [
      makeWithheld(),
      b4Withheld(),
      makeWithheld({ entity: null, reason: 'k1_no_baseline' }),
    ];
    render(<WithheldList withheld={withheld} />);

    const title = screen.getByText(DEFAULT_TITLE);
    expect(title.tagName).toBe('STRONG');
    expect(lineTexts(DEFAULT_TITLE)).toEqual(withheld.map((w) => withheldLine(w)));
    expect(lineTexts(DEFAULT_TITLE)[0]).toBe(
      'K1 · crm pricing comparison: clicks 30 of 37 required — 30 / 37 clicks',
    );
  });

  it('shows and names the list with the title the caller passes', () => {
    render(<WithheldList withheld={[b4Withheld()]} title={BUDGET_TITLE} />);

    expect(screen.getByText(BUDGET_TITLE).tagName).toBe('STRONG');
    expect(lineTexts(BUDGET_TITLE)).toEqual([
      'B4 · Brand search: 3 weekend days of 4 required — 3 / 4 weekend days',
    ]);
    expect(screen.queryByText(DEFAULT_TITLE)).toBeNull();
  });

  it('falls back to the default title when the given one is blank', () => {
    render(<WithheldList withheld={[makeWithheld()]} title="   " />);

    expect(screen.getByRole('list', { name: DEFAULT_TITLE })).toBeDefined();
    expect(screen.getByText(DEFAULT_TITLE)).toBeDefined();
  });

  it('renders repeated rule ids as separate lines without key warnings', () => {
    const consoleError = vi.spyOn(console, 'error');
    const withheld = [makeWithheld(), makeWithheld({ reason: 'clicks 12 of 37 required' })];
    render(<WithheldList withheld={withheld} />);

    expect(lineTexts(DEFAULT_TITLE)).toHaveLength(2);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('renders nothing, neither title nor list, when nothing is withheld', () => {
    const { container } = render(<WithheldList withheld={[]} title={BUDGET_TITLE} />);

    expect(container.childElementCount).toBe(0);
    expect(screen.queryByText(BUDGET_TITLE)).toBeNull();
  });
});
