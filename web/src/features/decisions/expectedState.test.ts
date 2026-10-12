/**
 * Unit tests for `expectedState.ts`, the wording of a decision's expected state that the
 * decision log's "Resulting expected state" cell and the recommendation card's "Decision
 * recorded: {state}" line share (AAP 0.5.2 decision shapes, AAP 0.5.5).
 *
 * Each export is held to its TSDoc:
 *
 * - `describeExpectedState` per decision shape (`own_budget`, `shared_member`, `budget`) and
 *   for acknowledged removals, with statuses as their labels, amounts in the subject's own
 *   currency, and a member's shared budget named from the log's budget names (the API's
 *   "Shared budget " prefix dropped), else by its Google id, else by its internal id;
 * - `statusLabel`, `textOrNull` and `FIELD_LABELS`, which `DecisionLog.tsx` reuses for its
 *   patch cells, and `PART_SEPARATOR`, which joins the parts of a state.
 *
 * The expected texts are the exact strings the decision log rendered before this module was
 * split out of it, so both screens keep wording a decision byte for byte as before.
 */
import { describe, expect, it } from 'vitest';

import type { Decision } from '../../api/types';
import { FIXTURE_BUDGET_IDS, makeDecision } from '../../test/fixtures';
import {
  FIELD_LABELS,
  PART_SEPARATOR,
  describeExpectedState,
  statusLabel,
  textOrNull,
} from './expectedState';

/** `decision` with its subject's other fields kept and its currency set to `currency`. */
function inCurrency(decision: Decision, currency: string | null): Decision {
  return { ...decision, subject: { ...decision.subject, currency } };
}

describe('describeExpectedState', () => {
  /** One decision row and the state it reads as, with no budget names given. */
  const CASES: readonly (readonly [description: string, decision: Decision, state: string])[] = [
    [
      'an own-budget campaign with a daily budget',
      makeDecision('own_budget'),
      'Active · daily budget USD\u00a0200.00',
    ],
    [
      'an own-budget campaign with both amounts, daily first',
      makeDecision('own_budget', {
        status: 'paused',
        daily_budget: '120.00',
        total_budget: '4500.00',
      }),
      'Paused · daily budget USD\u00a0120.00 · total budget USD\u00a04,500.00',
    ],
    [
      'an own-budget campaign with a lifetime budget only',
      makeDecision('own_budget', { daily_budget: null, total_budget: '9000.00' }),
      'Active · total budget USD\u00a09,000.00',
    ],
    [
      'an own-budget campaign with no amount',
      makeDecision('own_budget', { status: 'ended', daily_budget: null }),
      'Ended',
    ],
    [
      'an own-budget row without a status',
      makeDecision('own_budget', { status: null }),
      'status not reported · daily budget USD\u00a0200.00',
    ],
    [
      'an own-budget row whose status is outside the normalised set, shown as stored',
      makeDecision('own_budget', { status: ' archived ' as unknown as Decision['status'] }),
      'archived · daily budget USD\u00a0200.00',
    ],
    [
      'amounts in the currency of a EUR campaign',
      inCurrency(makeDecision('own_budget', { daily_budget: '100.00' }), 'EUR'),
      'Active · daily budget EUR\u00a0100.00',
    ],
    [
      'a shared member named by its Google budget id',
      makeDecision('shared_member'),
      'Active · shared budget 31000402',
    ],
    [
      'a shared member whose Google budget id is blank',
      makeDecision('shared_member', { status: 'paused', budget_external_id: '  ' }),
      'Paused · shared budget id 402',
    ],
    [
      'a shared member whose budget the API names in no way',
      makeDecision('shared_member', { budget_external_id: null }),
      'Active · shared budget id 402',
    ],
    [
      'a shared member with no budget assignment',
      makeDecision('shared_member', { budget_id: null, budget_external_id: null }),
      'Active',
    ],
    ['a shared daily budget', makeDecision('budget'), 'daily budget USD\u00a0500.00'],
    [
      'a shared budget on a custom period',
      makeDecision('budget', { daily_budget: null, total_budget: '9000.00' }),
      'total budget USD\u00a09,000.00',
    ],
    [
      'a shared budget with no amount recorded',
      makeDecision('budget', { daily_budget: null }),
      'no amount recorded',
    ],
    [
      'an acknowledged removal of a campaign',
      makeDecision('own_budget', { tombstone: true, status: 'ended' }),
      'Removal acknowledged · Ended',
    ],
    [
      'an acknowledged removal of a campaign whose status is not stored, read as ended',
      makeDecision('own_budget', { tombstone: true, status: null }),
      'Removal acknowledged · Ended',
    ],
    [
      'an acknowledged removal of a shared member, with its decided status',
      makeDecision('shared_member', { tombstone: true, status: 'paused' }),
      'Removal acknowledged · Paused',
    ],
    [
      'an acknowledged removal of a shared budget, with no status',
      makeDecision('budget', { tombstone: true, daily_budget: null }),
      'Removal acknowledged',
    ],
  ];

  it.each(CASES)('words %s', (_description, decision, state) => {
    expect(describeExpectedState(decision)).toBe(state);
  });

  it('names a member’s shared budget from the log, without the "Shared budget " prefix', () => {
    const names = new Map([[FIXTURE_BUDGET_IDS.search, 'Shared budget 4455']]);

    expect(describeExpectedState(makeDecision('shared_member'), names)).toBe(
      'Active · shared budget 4455',
    );
  });

  it('names a member’s shared budget by its full name when the name has no such prefix', () => {
    const names = new Map([[FIXTURE_BUDGET_IDS.search, 'Search Shared Budget']]);

    expect(describeExpectedState(makeDecision('shared_member'), names)).toBe(
      'Active · shared budget Search Shared Budget',
    );
  });

  it('keeps a name that is the prefix alone, which would otherwise read empty', () => {
    const names = new Map([[FIXTURE_BUDGET_IDS.search, 'Shared budget  ']]);

    expect(describeExpectedState(makeDecision('shared_member'), names)).toBe(
      'Active · shared budget Shared budget  ',
    );
  });

  it('falls back to the Google id when the log names only other budgets', () => {
    const names = new Map([[FIXTURE_BUDGET_IDS.display, 'Shared budget 9999']]);

    expect(describeExpectedState(makeDecision('shared_member'), names)).toBe(
      'Active · shared budget 31000402',
    );
  });

  it('reads the same with no budget names as with an empty map', () => {
    const decision = makeDecision('shared_member', { budget_external_id: null });

    expect(describeExpectedState(decision, new Map())).toBe(describeExpectedState(decision));
  });

  it('formats amounts without a currency code when the subject has none', () => {
    const decision = inCurrency(makeDecision('budget'), null);

    expect(describeExpectedState(decision)).toBe('daily budget 500');
  });
});

describe('statusLabel', () => {
  it.each([
    ['active', 'Active'],
    ['paused', 'Paused'],
    ['ended', 'Ended'],
  ])('labels the normalised status %s as %s', (status, label) => {
    expect(statusLabel(status)).toBe(label);
  });

  it('shows a status outside the normalised set as stored, trimmed', () => {
    expect(statusLabel('  removed ')).toBe('removed');
  });

  it.each([null, undefined, '', '   ', 3, {}])(
    'reads a missing or unreadable status (%o) as "status not reported"',
    (status) => {
      expect(statusLabel(status)).toBe('status not reported');
    },
  );

  it('does not label an inherited property name as a status', () => {
    expect(statusLabel('toString')).toBe('toString');
  });
});

describe('textOrNull', () => {
  it('trims a string that holds text', () => {
    expect(textOrNull('  Brand Search — US ')).toBe('Brand Search — US');
  });

  /** Values holding no text, each in its own tuple so `it.each` passes `[]` as one value. */
  const NO_TEXT: readonly (readonly [value: unknown])[] = [
    [null],
    [undefined],
    [''],
    [' \t\n'],
    [0],
    [12],
    [true],
    [[]],
    [{}],
  ];

  it.each(NO_TEXT)('reads %j as null', (value) => {
    expect(textOrNull(value)).toBeNull();
  });
});

describe('shared wording', () => {
  it('names each decision field as the log and the expected state show it', () => {
    expect(FIELD_LABELS).toEqual({
      status: 'status',
      daily_budget: 'daily budget',
      total_budget: 'total budget',
    });
    expect(Object.isFrozen(FIELD_LABELS)).toBe(true);
  });

  it('joins the parts of a state with a spaced middle dot', () => {
    expect(PART_SEPARATOR).toBe(' · ');
  });
});
