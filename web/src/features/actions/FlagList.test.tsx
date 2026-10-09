/**
 * Tests for `FlagList`, the flags row of a recommendation card (AAP 0.5.5: one badge per `Flag`
 * code of AAP 0.5.3, "as recorded in the run plus `current.flags_now`").
 *
 * The suite holds the module to its TSDoc contract:
 *
 * - {@link flagTone} gives the `warning` tone to exactly the codes in {@link WARNING_FLAG_CODES}
 *   (the item's numbers or conclusion cannot be fully trusted yet) and `info` to every other
 *   code, including one a later API version adds.
 * - {@link mergeFlags} keeps one flag per code: the recorded flags first in their order, then the
 *   codes only the overlay carries; a code in both keeps its recorded place and takes the current
 *   message, a blank current message never replaces a recorded label, and a code blank in both is
 *   labelled with the code itself. Inputs are never mutated.
 * - `FlagList` renders a list named "Flags" with one badge per merged code, labelled with the
 *   backend's own message, and renders nothing when no flag applies.
 *
 * Flags come from `FIXTURE_FLAGS`, whose messages are the ones the backend renders for each of
 * the fifteen codes, and from the default page's C1 card. The component makes no request, so
 * most cases render it without providers. One block renders it through `renderWithProviders`,
 * inside the Query client and router every recommendation card sits in, with `Flag` literals
 * written out in full: the "Simulated data" label it checks is the one the simulated-mode
 * end-to-end spec looks for on each affected card (AAP 0.3.3).
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Flag, FlagCode } from '../../api/types';
import {
  FIXTURE_FLAGS,
  FIXTURE_FLAG_CODES,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeFlag,
  makeRecommendationsPage,
} from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import { FlagList, WARNING_FLAG_CODES, flagTone, mergeFlags } from './FlagList';

/** The codes AAP 0.5.5 lists as trust warnings: data state, discrepancies, floors, pipeline. */
const WARNING_CODES: readonly FlagCode[] = [
  'stale',
  'incomplete',
  'short_window',
  'unverified_discrepancies',
  'attribution_floor',
  'creative_floor',
  'deal_floor',
  'unknown_crm_evidence',
  'pipeline_unavailable',
];

/** The codes that are context for reading an item, shown in the `info` tone. */
const INFO_CODES: readonly FlagCode[] = [
  'simulated',
  'meeting_time_proxy',
  'no_crm_link',
  'not_distinguishable',
  'search_term_coverage',
  'shared_budget',
];

/** The list items of the "Flags" list, in order. */
function flagItems(): HTMLElement[] {
  return within(screen.getByRole('list', { name: 'Flags' })).getAllByRole('listitem');
}

describe('flagTone', () => {
  it.each(WARNING_CODES)('renders %s as a warning', (code) => {
    expect(flagTone(code)).toBe('warning');
  });

  it.each(INFO_CODES)('renders %s as information', (code) => {
    expect(flagTone(code)).toBe('info');
  });

  it('renders a code a later API version adds as information', () => {
    expect(flagTone('budget_paced' as FlagCode)).toBe('info');
  });

  it('warns on exactly the documented codes, and the two tone sets cover every flag code', () => {
    expect([...WARNING_FLAG_CODES].sort()).toEqual([...WARNING_CODES].sort());
    expect([...WARNING_CODES, ...INFO_CODES].sort()).toEqual([...FIXTURE_FLAG_CODES].sort());
  });
});

describe('mergeFlags', () => {
  it('lists the recorded flags first, then the codes only the current set carries', () => {
    const merged = mergeFlags(
      [makeFlag('simulated'), makeFlag('no_crm_link')],
      [makeFlag('stale'), makeFlag('incomplete')],
    );

    expect(merged.map((flag) => flag.code)).toEqual([
      'simulated',
      'no_crm_link',
      'stale',
      'incomplete',
    ]);
  });

  it('keeps the recorded position of a code in both sets and shows its current message', () => {
    const merged = mergeFlags(
      [
        makeFlag('simulated'),
        makeFlag('unverified_discrepancies', {
          message: 'Unverified while 1 discrepancies are open',
        }),
      ],
      [
        makeFlag('unverified_discrepancies', {
          message: 'Unverified while 3 discrepancies are open',
        }),
        makeFlag('stale'),
      ],
    );

    expect(merged).toEqual([
      { code: 'simulated', message: 'Simulated data' },
      { code: 'unverified_discrepancies', message: 'Unverified while 3 discrepancies are open' },
      { code: 'stale', message: FIXTURE_FLAGS.stale.message },
    ]);
  });

  it('counts a repeated code once, at its first position, keeping its first label', () => {
    const merged = mergeFlags([
      makeFlag('simulated', { message: 'Simulated data (Google)' }),
      makeFlag('stale'),
      makeFlag('simulated', { message: 'Simulated data (LinkedIn)' }),
    ]);

    expect(merged).toEqual([
      { code: 'simulated', message: 'Simulated data (Google)' },
      { code: 'stale', message: FIXTURE_FLAGS.stale.message },
    ]);
  });

  it('lets a later repeat fill in a label that is still blank', () => {
    const merged = mergeFlags(
      [makeFlag('shared_budget', { message: '  ' }), makeFlag('shared_budget')],
      [makeFlag('stale', { message: '' }), makeFlag('stale', { message: 'Data as of 05:31' })],
    );

    expect(merged).toEqual([
      { code: 'shared_budget', message: FIXTURE_FLAGS.shared_budget.message },
      { code: 'stale', message: 'Data as of 05:31' },
    ]);
  });

  it('never lets a blank current message replace a recorded label', () => {
    const merged = mergeFlags(
      [makeFlag('short_window')],
      [makeFlag('short_window', { message: ' ' })],
    );

    expect(merged).toEqual([{ code: 'short_window', message: FIXTURE_FLAGS.short_window.message }]);
  });

  it('labels a code with the code itself when its message is blank in both sets', () => {
    const merged = mergeFlags(
      [makeFlag('deal_floor', { message: '' })],
      [makeFlag('deal_floor', { message: '   ' })],
    );

    expect(merged).toEqual([{ code: 'deal_floor', message: 'deal_floor' }]);
  });

  it('defaults the current set to none and returns new objects without mutating its inputs', () => {
    const recorded: readonly Flag[] = Object.freeze([Object.freeze(makeFlag('simulated'))]);

    const merged = mergeFlags(recorded);

    expect(merged).toEqual([{ code: 'simulated', message: 'Simulated data' }]);
    expect(merged[0]).not.toBe(recorded[0]);
    expect(mergeFlags([], [])).toEqual([]);
  });
});

describe('FlagList', () => {
  it('renders one badge per code, labelled with the backend message, toned by the code', () => {
    const flags = FIXTURE_FLAG_CODES.map((code) => makeFlag(code));
    render(<FlagList flags={flags} />);

    const items = flagItems();
    expect(items.map((item) => item.textContent)).toEqual(
      FIXTURE_FLAG_CODES.map((code) => FIXTURE_FLAGS[code].message),
    );
    FIXTURE_FLAG_CODES.forEach((code, index) => {
      const badge = items[index]?.firstElementChild;
      expect(badge?.className).toBe(`ds-badge ds-badge--${flagTone(code)}`);
    });
  });

  it('shows the recorded flags plus the current overlay of a card, one chip per code', () => {
    const item = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.c1);
    render(<FlagList flags={item.flags} current={item.current.flags_now} />);

    expect(flagItems().map((entry) => entry.textContent)).toEqual([
      'Simulated data (Google)',
      FIXTURE_FLAGS.no_crm_link.message,
      'Unverified while 1 discrepancies are open',
    ]);
  });

  it('renders the current flags alone when the run recorded none', () => {
    render(<FlagList flags={[]} current={[makeFlag('stale')]} />);

    expect(flagItems().map((entry) => entry.textContent)).toEqual([FIXTURE_FLAGS.stale.message]);
    expect(screen.getByText(FIXTURE_FLAGS.stale.message).className).toBe(
      'ds-badge ds-badge--warning',
    );
  });

  it('renders nothing, not even an empty list, when no flag applies', () => {
    const { container } = render(<FlagList flags={[]} />);

    expect(container.childElementCount).toBe(0);
    expect(screen.queryByRole('list', { name: 'Flags' })).toBeNull();
  });
});

describe('FlagList inside the app providers', () => {
  /**
   * A simulated card's flags as its run recorded them, when one discrepancy was open.
   */
  const RECORDED = [
    { code: 'simulated', message: 'Simulated data' } satisfies Flag,
    {
      code: 'unverified_discrepancies',
      message: 'Unverified while 1 discrepancies are open',
    } satisfies Flag,
  ];

  /**
   * The same card's overlay at read time: a second discrepancy has opened since the run, and a
   * dataset it reads has turned stale.
   */
  const CURRENT = [
    {
      code: 'unverified_discrepancies',
      message: 'Unverified while 2 discrepancies are open',
    } satisfies Flag,
    { code: 'stale', message: 'Stale' } satisfies Flag,
  ];

  it('merges recorded and current flags into one badge per code, showing the live count', async () => {
    await renderWithProviders(<FlagList flags={RECORDED} current={CURRENT} />);

    const items = flagItems();
    expect(items).toHaveLength(3);
    expect(items.map((item) => item.textContent)).toEqual([
      'Simulated data',
      'Unverified while 2 discrepancies are open',
      'Stale',
    ]);
    expect(items.map((item) => item.firstElementChild?.getAttribute('class'))).toEqual([
      'ds-badge ds-badge--info',
      'ds-badge ds-badge--warning',
      'ds-badge ds-badge--warning',
    ]);
    // The code both sets carry appears once, with the overlay's message; the frozen count is gone.
    expect(screen.getAllByText(/^Unverified while /u)).toHaveLength(1);
    expect(screen.queryByText('Unverified while 1 discrepancies are open')).toBeNull();
  });

  it('shows the "Simulated data" label inside the list named "Flags"', async () => {
    await renderWithProviders(<FlagList flags={RECORDED} current={CURRENT} />);

    const list = screen.getByRole('list', { name: 'Flags' });
    const label = screen.getByText('Simulated data');
    expect(list.contains(label)).toBe(true);
    expect(within(list).getByText('Simulated data')).toBe(label);
    expect(label.getAttribute('class')).toBe('ds-badge ds-badge--info');
  });

  it('renders no "Flags" list when neither set carries a flag', async () => {
    expect(mergeFlags([], [])).toEqual([]);

    await renderWithProviders(<FlagList flags={[]} current={[]} />);

    expect(screen.queryByRole('list', { name: 'Flags' })).toBeNull();
    expect(screen.queryByRole('listitem')).toBeNull();
  });
});
