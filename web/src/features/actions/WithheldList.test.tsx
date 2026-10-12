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
 * - {@link withheldReason} gives the one-sentence reason of an empty section or sub-list from the
 *   leading line (AAP 0.5.5: "No search term met the K1 minimum of 37 clicks"): the shared
 *   recorded minimum when every line of its rule and entity type agrees on it and records an
 *   observed size strictly below it (compared exactly, decimal strings included), else "{rule}
 *   withheld[ for {entity}]: {reason}", else the line itself, and `null` for nothing withheld.
 * - `WithheldList` renders its title above a list it names with the same text, one line per
 *   pattern in API order, and nothing at all when nothing is withheld. The title is the given
 *   one; without one, "Withheld for sample size" when every line records observed strictly below
 *   required, else "Withheld".
 * - A list longer than `ROW_BATCH_SIZE` mounts one batch at a time, followed after the list by
 *   the polite status and a "Show {n} more withheld patterns" button; each reveal moves focus to
 *   its first line, and every line stays reachable. A list at or under one batch renders exactly
 *   as unbatched.
 *
 * Withheld patterns come from `makeWithheld` (K1: 30 of 37 clicks) or are built in the test in
 * the backend's shape (`schemas.Withheld`). The component makes no request, but it renders
 * through `renderWithProviders`, inside the query client and router every section that hosts it
 * runs under, so the list is checked in the tree it actually ships in.
 */
import { fireEvent, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { EntityView, Withheld } from '../../api/types';
import { makeWithheld } from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import { WithheldList, withheldLine, withheldReason } from './WithheldList';
import { ROW_BATCH_SIZE } from './batches';

/**
 * The default title, when the caller passes none or a blank one and every line records observed
 * below required.
 */
const DEFAULT_TITLE = 'Withheld for sample size';

/** The default title when some line records no sample shortfall. */
const NEUTRAL_TITLE = 'Withheld';

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

/**
 * `count` K1 search terms withheld at 30 of 37 clicks, each its own term ("term 1" …), in API
 * order.
 */
function k1Terms(count: number): Withheld[] {
  return Array.from({ length: count }, (_, index) => {
    const name = `term ${String(index + 1)}`;
    return makeWithheld({ entity: entity({ type: 'search_term', id: name, name }) });
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
      'B4 · Brand search: 3 weekend days of 4 required — 3\u00A0/\u00A04 weekend days',
    );
    expect(withheldLine(makeWithheld())).toBe(
      'K1 · crm pricing comparison: clicks 30 of 37 required — 30\u00A0/\u00A037 clicks',
    );
  });

  it('keeps the observed / required figure unbroken: no-break spaces around the slash', () => {
    const line = withheldLine(
      b4Withheld({
        reason: 'known-budget days 0 of 7 required',
        sample: { observed: 0, required: 7, unit: 'known-budget days' },
      }),
    );

    expect(line).toBe(
      'B4 · Brand search: known-budget days 0 of 7 required — 0\u00A0/\u00A07 known-budget days',
    );
    expect(line).not.toContain(' / ');
  });

  it('formats sizes as counts: grouped, fractions kept, a missing size unavailable', () => {
    expect(
      withheldLine(b4Withheld({ sample: { observed: 1234, required: 3000, unit: 'clicks' } })),
    ).toBe('B4 · Brand search: 3 weekend days of 4 required — 1,234\u00A0/\u00A03,000 clicks');
    expect(
      withheldLine(
        b4Withheld({
          reason: '2.9 expected outcomes of 3 required',
          sample: { observed: '2.900000', required: 3, unit: 'expected outcomes' },
        }),
      ),
    ).toBe(
      'B4 · Brand search: 2.9 expected outcomes of 3 required — ' +
        '2.9\u00A0/\u00A03 expected outcomes',
    );
    expect(
      withheldLine(b4Withheld({ sample: { observed: 9, required: null, unit: 'conversions' } })),
    ).toBe(
      'B4 · Brand search: 3 weekend days of 4 required — 9\u00A0/\u00A0unavailable conversions',
    );
  });

  it('leaves the unit out when none is recorded', () => {
    expect(withheldLine(b4Withheld({ sample: { observed: 3, required: 4, unit: null } }))).toBe(
      'B4 · Brand search: 3 weekend days of 4 required — 3\u00A0/\u00A04',
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
      'B4 · Brand search — 3\u00A0/\u00A04 weekend days',
    );
  });

  it('trims the rule id and the reason', () => {
    expect(withheldLine(b4Withheld({ rule_id: ' B4 ', reason: '  no exposure  ' }))).toBe(
      'B4 · Brand search: no exposure — 3\u00A0/\u00A04 weekend days',
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
      `B4 · ${expected}: 3 weekend days of 4 required — 3\u00A0/\u00A04 weekend days`,
    );
  });

  it('accepts an entity recorded as plain text, and reads a blank one as account-wide', () => {
    // The contract types `entity` as an EntityView or null; the component also accepts a label
    // recorded as text, which only a cast can express here.
    const asText = (text: string) => text as unknown as Withheld['entity'];

    expect(withheldLine(b4Withheld({ entity: asText('Display Shared Budget') }))).toBe(
      'B4 · Display Shared Budget: 3 weekend days of 4 required — 3\u00A0/\u00A04 weekend days',
    );
    expect(withheldLine(b4Withheld({ entity: asText('  ') }))).toBe(
      'B4 · Account-wide: 3 weekend days of 4 required — 3\u00A0/\u00A04 weekend days',
    );
  });

  it('never prints "null" or "undefined"', () => {
    const line = withheldLine(
      makeWithheld({
        entity: { source: null, type: null, id: null, name: null },
        sample: { observed: null, required: 7, unit: null },
      }),
    );

    expect(line).toBe('K1 · Account-wide: clicks 30 of 37 required — unavailable\u00A0/\u00A07');
    expect(line).not.toMatch(/null|undefined/);
  });
});

describe('withheldReason', () => {
  /** A K1 search term withheld at `observed` clicks of `required`, as the backend records it. */
  function k1Term(name: string, observed: number, required: Withheld['sample']['required']) {
    return makeWithheld({
      entity: { source: 'google', type: 'search_term', id: `61000201|7700112|${name}`, name },
      reason: `clicks ${String(observed)} of ${String(required)} required`,
      sample: { observed, required, unit: 'clicks' },
    });
  }

  it('states the shared recorded minimum when every line of the rule and type agrees on it', () => {
    expect(withheldReason([makeWithheld()])).toBe('No search term met the K1 minimum of 37 clicks');
    // The run-8 shape: three K1 terms at 70 clicks required, followed by K2 and K3 lines.
    expect(
      withheldReason([
        k1Term('free attribution software download', 44, 70),
        k1Term('attribution modeling', 28, 70),
        k1Term('rival attribution suite alternative', 24, 70),
        makeWithheld({
          rule_id: 'K2',
          reason: 'conversions 1 of 3 required',
          sample: { observed: 1, required: 3, unit: 'conversions' },
        }),
        makeWithheld({
          rule_id: 'K3',
          entity: null,
          reason: 'window 7 days of 14 required',
          sample: { observed: 7, required: 14, unit: 'days' },
        }),
      ]),
    ).toBe('No search term met the K1 minimum of 70 clicks');
    // A B1 campaign line, its underscored type written with spaces.
    expect(
      withheldReason([
        b4Withheld({
          rule_id: 'B1',
          entity: entity({ type: 'shared_budget' }),
          reason: 'known-budget days 0 of 7 required',
          sample: { observed: 0, required: 7, unit: 'known-budget days' },
        }),
      ]),
    ).toBe('No shared budget met the B1 minimum of 7 known-budget days');
  });

  it('counts the minimum as the sample line does: grouped, fractions kept', () => {
    expect(withheldReason([k1Term('crm', 900, 1234), k1Term('crm demo', 12, 1234)])).toBe(
      'No search term met the K1 minimum of 1,234 clicks',
    );
    expect(
      withheldReason([
        b4Withheld({
          rule_id: 'C1',
          reason: '2.4 expected outcomes of 2.9 required',
          sample: { observed: '2.400000', required: '2.900000', unit: 'expected outcomes' },
        }),
      ]),
    ).toBe('No campaign met the C1 minimum of 2.9 expected outcomes');
  });

  it('falls back to the leading reason when the lines of its rule and type record different minimums', () => {
    expect(withheldReason([k1Term('crm', 30, 37), k1Term('crm demo', 12, 40)])).toBe(
      'K1 withheld for crm: clicks 30 of 37 required',
    );
    // A different unit on a later line of the same rule and type is a different minimum too.
    expect(
      withheldReason([
        b4Withheld(),
        b4Withheld({ sample: { observed: 5, required: 4, unit: 'weekdays' } }),
      ]),
    ).toBe('B4 withheld for Brand search: 3 weekend days of 4 required');
  });

  it('ignores lines of another rule or entity type when it checks the minimum', () => {
    expect(
      withheldReason([
        k1Term('crm', 30, 37),
        makeWithheld({ rule_id: 'K2', sample: { observed: 1, required: 3, unit: 'conversions' } }),
        makeWithheld({
          entity: null,
          reason: 'fewer than 10 baseline conversions',
          sample: { observed: 9, required: 10, unit: 'conversions' },
        }),
      ]),
    ).toBe('No search term met the K1 minimum of 37 clicks');
  });

  it('states the reason of an account-wide or account line, never "No account met"', () => {
    expect(
      withheldReason([
        makeWithheld({
          rule_id: 'K3',
          entity: null,
          reason: 'window 7 days of 14 required',
          sample: { observed: 7, required: 14, unit: 'days' },
        }),
      ]),
    ).toBe('K3 withheld: window 7 days of 14 required');
    expect(
      withheldReason([
        makeWithheld({
          entity: entity({ type: 'account', name: 'Acme Corp — Google Ads' }),
          reason: 'fewer than 10 baseline conversions',
          sample: { observed: 9, required: 10, unit: 'conversions' },
        }),
      ]),
    ).toBe('K1 withheld for Acme Corp — Google Ads: fewer than 10 baseline conversions');
    // An entity carrying nothing reads as account-wide, as withheldLine names it.
    expect(
      withheldReason([
        makeWithheld({ entity: { source: null, type: null, id: null, name: null } }),
      ]),
    ).toBe('K1 withheld: clicks 30 of 37 required');
  });

  it('states the reason when no minimum or unit is recorded', () => {
    expect(
      withheldReason([
        makeWithheld({
          rule_id: 'C3a',
          entity: entity(),
          reason: 'Segment window not yet available',
          sample: { observed: null, required: null, unit: 'segment windows' },
        }),
      ]),
    ).toBe('C3a withheld for Brand search: Segment window not yet available');
    expect(withheldReason([b4Withheld({ sample: { observed: 3, required: 4, unit: '  ' } })])).toBe(
      'B4 withheld for Brand search: 3 weekend days of 4 required',
    );
    // A size that is not a count (text, or a non-finite number) is no readable minimum.
    for (const required of ['n/a', Number.POSITIVE_INFINITY]) {
      expect(
        withheldReason([b4Withheld({ sample: { observed: 3, required, unit: 'weekend days' } })]),
      ).toBe('B4 withheld for Brand search: 3 weekend days of 4 required');
    }
  });

  it('gives the leading line itself when it has neither a shared minimum nor a reason', () => {
    const blank = b4Withheld({
      reason: '  ',
      sample: { observed: 3, required: null, unit: 'weekend days' },
    });
    expect(withheldReason([blank, b4Withheld()])).toBe(withheldLine(blank));
    expect(withheldReason([blank])).toBe(
      'B4 · Brand search — 3\u00A0/\u00A0unavailable weekend days',
    );
  });

  it('gives the leading line itself when its rule id is blank', () => {
    const unnamed = makeWithheld({ rule_id: '  ' });
    expect(withheldReason([unnamed])).toBe(withheldLine(unnamed));
  });

  describe('states a minimum only when every line of its rule and type falls short of it', () => {
    /** The B4 reason `analysis/budget.py` records when the rate test fails on an adequate sample. */
    const NOT_DISTINGUISHABLE =
      'weekend and weekday rates not distinguishable (two-sample Poisson rate test p = 0.1195, ' +
      'alpha 0.05): 8 weekend days, 20 weekdays, 264 outcomes';

    /** A LinkedIn campaign's B4 line in `outcomes`, at `observed` of `required` outcomes. */
    function b4Outcomes(
      name: string,
      reason: string,
      observed: Withheld['sample']['observed'],
      required: Withheld['sample']['required'] = 10,
    ): Withheld {
      return b4Withheld({
        entity: entity({ source: 'linkedin', id: name, name }),
        reason,
        sample: { observed, required, unit: 'outcomes' },
      });
    }

    it('states the B4 reason when the line meets its minimum and the rate test failed', () => {
      // The backend's record: 264 outcomes observed against the 10 required, so no minimum was
      // missed; B4 is withheld because the weekend and weekday rates are not distinguishable.
      expect(
        withheldReason([b4Outcomes('Capped - Lead gen', NOT_DISTINGUISHABLE, 264)]),
      ).toBe(`B4 withheld for Capped - Lead gen: ${NOT_DISTINGUISHABLE}`);
    });

    it('states the reason when the observed size equals the required one, in any form', () => {
      const reason = 'weekend and weekday rates not distinguishable: 10 outcomes';
      expect(withheldReason([b4Outcomes('Brand search', reason, 10)])).toBe(
        `B4 withheld for Brand search: ${reason}`,
      );
      // A decimal string and a number that denote the same size are equal, however written.
      for (const observed of ['10', '10.000000', '1e1', '100e-1', ' +10 ']) {
        expect(withheldReason([b4Outcomes('Brand search', reason, observed)])).toBe(
          `B4 withheld for Brand search: ${reason}`,
        );
      }
    });

    it('states the reason when the observed size is missing or unreadable', () => {
      const reason = '3 weekend days of 4 required';
      const expected = `B4 withheld for Brand search: ${reason}`;
      // The contract records an absent size as null; an omitted key, text or a non-finite
      // number is no readable size either, which only a cast can express here.
      const unreadable = [
        null,
        undefined,
        'n/a',
        '',
        Number.NaN,
        Number.POSITIVE_INFINITY,
        true,
      ] as unknown as Withheld['sample']['observed'][];
      for (const observed of unreadable) {
        expect(
          withheldReason([b4Withheld({ sample: { observed, required: 4, unit: 'weekend days' } })]),
        ).toBe(expected);
      }
      const withoutObserved = { required: 4, unit: 'weekend days' } as unknown as Withheld['sample'];
      expect(withheldReason([b4Withheld({ sample: withoutObserved })])).toBe(expected);
    });

    it('states the leading reason when one line falls short and another of its rule and type does not', () => {
      const short = b4Outcomes('Brand search', '6 outcomes of 10 required', 6);
      const met = b4Outcomes('Capped - Lead gen', NOT_DISTINGUISHABLE, 264);
      expect(withheldReason([short, met])).toBe(
        'B4 withheld for Brand search: 6 outcomes of 10 required',
      );
      expect(withheldReason([met, short])).toBe(
        `B4 withheld for Capped - Lead gen: ${NOT_DISTINGUISHABLE}`,
      );
      // A line with no readable observed size leaves the minimum unsubstantiated as well.
      expect(withheldReason([short, b4Outcomes('Display', 'outcomes unavailable', null)])).toBe(
        'B4 withheld for Brand search: 6 outcomes of 10 required',
      );
      // A line of another rule or entity type that meets its own minimum does not count.
      expect(
        withheldReason([
          short,
          makeWithheld({ rule_id: 'K2', sample: { observed: 5, required: 3, unit: 'conversions' } }),
          b4Withheld({ entity: null, sample: { observed: 12, required: 10, unit: 'outcomes' } }),
        ]),
      ).toBe('No campaign met the B4 minimum of 10 outcomes');
    });

    it('compares decimal-string sizes exactly, never through a rounded float', () => {
      const c1 = (observed: string, required: string) =>
        b4Withheld({
          rule_id: 'C1',
          reason: `${observed} expected outcomes of ${required} required`,
          sample: { observed, required, unit: 'expected outcomes' },
        });
      expect(withheldReason([c1('2.500000', '3')])).toBe(
        'No campaign met the C1 minimum of 3 expected outcomes',
      );
      expect(withheldReason([c1('25e-1', '3.000000')])).toBe(
        'No campaign met the C1 minimum of 3 expected outcomes',
      );
      // Number() reads this size as exactly 3; digit by digit it is still below 3.
      expect(withheldReason([c1('2.99999999999999999999', '3')])).toBe(
        'No campaign met the C1 minimum of 3 expected outcomes',
      );
      // Above the minimum by less than a double resolves, or above it written with leading zeros.
      expect(withheldReason([c1('3.00000000000000000001', '3')])).toBe(
        'C1 withheld for Brand search: 3.00000000000000000001 expected outcomes of 3 required',
      );
      expect(withheldReason([c1('003.5', '3')])).toBe(
        'C1 withheld for Brand search: 003.5 expected outcomes of 3 required',
      );
      // Zero observed falls short of a positive minimum; a zero minimum is never missed.
      expect(withheldReason([c1('0.000000', '0.5')])).toBe(
        'No campaign met the C1 minimum of 0.5 expected outcomes',
      );
      expect(withheldReason([c1('0', '0.000')])).toBe(
        'C1 withheld for Brand search: 0 expected outcomes of 0.000 required',
      );
      // Signs order before magnitudes, and a larger negative magnitude is the smaller size.
      expect(withheldReason([c1('-5', '-2')])).toBe(
        'No campaign met the C1 minimum of -2 expected outcomes',
      );
      expect(withheldReason([c1('-2', '-5')])).toBe(
        'C1 withheld for Brand search: -2 expected outcomes of -5 required',
      );
      expect(withheldReason([c1('-1', '0.5')])).toBe(
        'No campaign met the C1 minimum of 0.5 expected outcomes',
      );
      // An exponent past a safe integer is no readable size.
      expect(withheldReason([c1('1e-99999999999999999999', '3')])).toBe(
        'C1 withheld for Brand search: 1e-99999999999999999999 expected outcomes of 3 required',
      );
    });

    it('keeps the minimum of K1 search terms and B1 campaigns that all fall short (run 3)', () => {
      expect(withheldReason([k1Term('crm pricing', 25, 69), k1Term('crm demo', 15, 69)])).toBe(
        'No search term met the K1 minimum of 69 clicks',
      );
      const b1 = (name: string) =>
        b4Withheld({
          rule_id: 'B1',
          entity: entity({ id: name, name }),
          reason: 'window 4 days of 7 required',
          sample: { observed: 4, required: 7, unit: 'days' },
        });
      expect(
        withheldReason([b1('Brand - Search'), b1('Benchmarks - Lead gen (NA)'), b1('Competitor')]),
      ).toBe('No campaign met the B1 minimum of 7 days');
    });
  });

  it('returns null when nothing is withheld', () => {
    expect(withheldReason([])).toBeNull();
  });
});

describe('WithheldList', () => {
  it('renders a B4 line with rule, entity, reason and sample, as withheldLine', async () => {
    const b4 = makeWithheld({
      rule_id: 'B4',
      entity: entity(),
      reason: 'weekend days',
      sample: { observed: 3, required: 4, unit: 'weekend days' },
    });
    await renderWithProviders(<WithheldList withheld={[b4]} title={BUDGET_TITLE} />);

    const [line] = lineTexts(BUDGET_TITLE);
    expect(line).toContain('B4 ·');
    expect(line).toContain('Brand search');
    expect(line).toContain('weekend days');
    expect(line).toContain('3\u00A0/\u00A04 weekend days');
    expect(line).toBe(withheldLine(b4));
    expect(line).toBe('B4 · Brand search: weekend days — 3\u00A0/\u00A04 weekend days');
  });

  it('lists each withheld pattern under the default title, in API order', async () => {
    const withheld = [
      makeWithheld(),
      b4Withheld(),
      makeWithheld({ entity: null, reason: 'k1_no_baseline' }),
    ];
    await renderWithProviders(<WithheldList withheld={withheld} />);

    const title = screen.getByText(DEFAULT_TITLE);
    expect(title.tagName).toBe('STRONG');
    expect(lineTexts(DEFAULT_TITLE)).toEqual(withheld.map((w) => withheldLine(w)));
    expect(lineTexts(DEFAULT_TITLE)[0]).toBe(
      'K1 · crm pricing comparison: clicks 30 of 37 required — 30\u00A0/\u00A037 clicks',
    );
  });

  it('keeps two entries in the order given, whichever comes first', async () => {
    const first = b4Withheld();
    const second = makeWithheld();
    const { unmount } = await renderWithProviders(<WithheldList withheld={[first, second]} />);
    expect(lineTexts(DEFAULT_TITLE)).toEqual([withheldLine(first), withheldLine(second)]);

    unmount();
    await renderWithProviders(<WithheldList withheld={[second, first]} />);
    expect(lineTexts(DEFAULT_TITLE)).toEqual([withheldLine(second), withheldLine(first)]);
  });

  it('shows and names the list with the title the caller passes', async () => {
    await renderWithProviders(<WithheldList withheld={[b4Withheld()]} title={BUDGET_TITLE} />);

    expect(screen.getByText(BUDGET_TITLE).tagName).toBe('STRONG');
    expect(lineTexts(BUDGET_TITLE)).toEqual([
      'B4 · Brand search: 3 weekend days of 4 required — 3\u00A0/\u00A04 weekend days',
    ]);
    expect(screen.queryByText(DEFAULT_TITLE)).toBeNull();
  });

  it('falls back to the default title when the given one is blank', async () => {
    await renderWithProviders(<WithheldList withheld={[makeWithheld()]} title="   " />);

    expect(screen.getByRole('list', { name: DEFAULT_TITLE })).toBeDefined();
    expect(screen.getByText(DEFAULT_TITLE)).toBeDefined();
  });

  it('renders repeated rule ids as separate lines without key warnings', async () => {
    const consoleError = vi.spyOn(console, 'error');
    const withheld = [makeWithheld(), makeWithheld({ reason: 'clicks 12 of 37 required' })];
    await renderWithProviders(<WithheldList withheld={withheld} />);

    expect(lineTexts(DEFAULT_TITLE)).toHaveLength(2);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('renders nothing, neither title nor list, when nothing is withheld', async () => {
    const { container } = await renderWithProviders(
      <WithheldList withheld={[]} title={BUDGET_TITLE} />,
    );

    expect(screen.queryByText(BUDGET_TITLE)).toBeNull();
    expect(screen.queryByText(DEFAULT_TITLE)).toBeNull();
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);
    expect(container.textContent).toBe('');
  });
});

describe('WithheldList: bounded batches', () => {
  it('mounts the first batch in API order, then the status and the button after the list', async () => {
    const withheld = k1Terms(120);
    await renderWithProviders(<WithheldList withheld={withheld} />);

    const list = screen.getByRole('list', { name: DEFAULT_TITLE });
    expect(ROW_BATCH_SIZE).toBe(50);
    expect(lineTexts(DEFAULT_TITLE)).toEqual(withheld.slice(0, 50).map((w) => withheldLine(w)));
    // The polite status (never an alert) and the button follow the list, outside the <ul>.
    const status = screen.getByRole('status');
    expect(status.textContent).toBe('Showing 50 of 120 withheld patterns');
    expect(screen.queryByRole('alert')).toBeNull();
    const button = screen.getByRole('button', { name: 'Show 50 more withheld patterns' });
    expect(list.contains(status)).toBe(false);
    expect(list.contains(button)).toBe(false);
    expect(within(list).queryByRole('button')).toBeNull();
    expect((list.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(
      true,
    );
    expect((status.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(
      true,
    );
    // Every child of the list is a line.
    expect([...list.children].every((child) => child.tagName === 'LI')).toBe(true);
  });

  it('reveals the next batch on each press, moves focus to its first line and keeps every line reachable', async () => {
    const withheld = k1Terms(120);
    await renderWithProviders(<WithheldList withheld={withheld} />);

    fireEvent.click(screen.getByRole('button', { name: 'Show 50 more withheld patterns' }));
    let lines = within(screen.getByRole('list', { name: DEFAULT_TITLE })).getAllByRole(
      'listitem',
    );
    expect(lines).toHaveLength(100);
    expect(document.activeElement).toBe(lines[50]);
    expect(document.activeElement?.textContent).toBe(withheldLine(withheld[50] as Withheld));
    expect(screen.getByRole('status').textContent).toBe('Showing 100 of 120 withheld patterns');

    fireEvent.click(screen.getByRole('button', { name: 'Show 20 more withheld patterns' }));
    lines = within(screen.getByRole('list', { name: DEFAULT_TITLE })).getAllByRole('listitem');
    expect(lines).toHaveLength(120);
    expect(document.activeElement).toBe(lines[100]);
    // Every line is shown, in API order, with its observed and required sizes.
    expect(lineTexts(DEFAULT_TITLE)).toEqual(withheld.map((w) => withheldLine(w)));
    expect(lines[119]?.textContent).toBe(
      'K1 · term 120: clicks 30 of 37 required — 30\u00A0/\u00A037 clicks',
    );
    // The button is gone once every line is shown; the status stays and says so.
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByRole('status').textContent).toBe('Showing all 120 withheld patterns');
  });

  it('bounds the list one line past the batch size, with the singular noun', async () => {
    await renderWithProviders(<WithheldList withheld={k1Terms(ROW_BATCH_SIZE + 1)} />);

    expect(lineTexts(DEFAULT_TITLE)).toHaveLength(ROW_BATCH_SIZE);
    expect(screen.getByRole('status').textContent).toBe('Showing 50 of 51 withheld patterns');
    fireEvent.click(screen.getByRole('button', { name: 'Show 1 more withheld pattern' }));
    expect(lineTexts(DEFAULT_TITLE)).toHaveLength(ROW_BATCH_SIZE + 1);
    expect(document.activeElement?.textContent).toBe(
      'K1 · term 51: clicks 30 of 37 required — 30\u00A0/\u00A037 clicks',
    );
  });

  it('renders a list at the batch size exactly as unbatched: every line, no id, no control', async () => {
    const withheld = k1Terms(ROW_BATCH_SIZE);
    const { container } = await renderWithProviders(
      <WithheldList withheld={withheld} title={BUDGET_TITLE} />,
    );

    expect(lineTexts(BUDGET_TITLE)).toEqual(withheld.map((w) => withheldLine(w)));
    expect(container.querySelector('li[id]')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('WithheldList: the title chosen from the lines', () => {
  /** C1b withheld in the crm_unavailable world: no sizes recorded. */
  const c1bPipeline = makeWithheld({
    rule_id: 'C1b',
    entity: entity({ source: 'linkedin', id: '5101', name: 'Benchmarks - Lead gen (NA)' }),
    reason: 'pipeline unavailable: Qualified Lead count unavailable',
    sample: { observed: null, required: null, unit: 'contacts' },
  });

  /** The visible title and the list's accessible name. */
  function titleAndName(): [string | null, string | null] {
    return [
      document.querySelector('strong')?.textContent ?? null,
      screen.getByRole('list').getAttribute('aria-label'),
    ];
  }

  it('reads "Withheld" when a line is withheld for another reason than sample size', async () => {
    await renderWithProviders(<WithheldList withheld={[makeWithheld(), c1bPipeline]} />);

    expect(titleAndName()).toEqual([NEUTRAL_TITLE, NEUTRAL_TITLE]);
    expect(lineTexts(NEUTRAL_TITLE)).toEqual([
      withheldLine(makeWithheld()),
      'C1b · Benchmarks - Lead gen (NA): pipeline unavailable: Qualified Lead count unavailable',
    ]);
    expect(screen.queryByText(DEFAULT_TITLE)).toBeNull();
  });

  it('records no shortfall at or above the minimum, or with a missing or unreadable size', async () => {
    const lines: Withheld[] = [
      // A B4 rate test that failed with the outcomes minimum met.
      b4Withheld({
        reason: 'weekend and weekday rates not distinguishable',
        sample: { observed: 264, required: 10, unit: 'outcomes' },
      }),
      // At the minimum exactly.
      b4Withheld({ sample: { observed: 4, required: 4, unit: 'weekend days' } }),
      // K1 account-wide without a baseline: no observed size (run 10).
      makeWithheld({
        entity: null,
        reason: 'baseline unavailable: no Google campaign facts in the trailing 90 days',
        sample: { observed: null, required: 10, unit: 'conversions' },
      }),
      // An unreadable required size.
      b4Withheld({ sample: { observed: 3, required: 'n/a', unit: 'weekend days' } }),
    ];
    for (const line of lines) {
      const { unmount } = await renderWithProviders(<WithheldList withheld={[line]} />);
      expect(titleAndName()).toEqual([NEUTRAL_TITLE, NEUTRAL_TITLE]);
      unmount();
    }
  });

  it('keeps "Withheld for sample size" when every line falls short, decimal strings included', async () => {
    const withheld = [
      makeWithheld(),
      b4Withheld({
        rule_id: 'C1',
        reason: '2.9 expected outcomes of 3 required',
        sample: { observed: '2.900000', required: 3, unit: 'expected outcomes' },
      }),
    ];
    await renderWithProviders(<WithheldList withheld={withheld} />);

    expect(titleAndName()).toEqual([DEFAULT_TITLE, DEFAULT_TITLE]);
  });

  it('chooses the title from every line, those beyond the first batch included', async () => {
    const withheld = k1Terms(120);
    withheld.splice(100, 0, c1bPipeline);
    await renderWithProviders(<WithheldList withheld={withheld} />);

    expect(lineTexts(NEUTRAL_TITLE)).toHaveLength(ROW_BATCH_SIZE);
    fireEvent.click(screen.getByRole('button', { name: 'Show 50 more withheld patterns' }));
    expect(titleAndName()).toEqual([NEUTRAL_TITLE, NEUTRAL_TITLE]);
  });

  it('keeps a title the caller passes whatever the lines record, and falls back from a blank one', async () => {
    const { unmount } = await renderWithProviders(
      <WithheldList withheld={[c1bPipeline]} title={BUDGET_TITLE} />,
    );
    expect(titleAndName()).toEqual([BUDGET_TITLE, BUDGET_TITLE]);
    unmount();

    await renderWithProviders(<WithheldList withheld={[c1bPipeline]} title="  " />);
    expect(titleAndName()).toEqual([NEUTRAL_TITLE, NEUTRAL_TITLE]);
  });
});
