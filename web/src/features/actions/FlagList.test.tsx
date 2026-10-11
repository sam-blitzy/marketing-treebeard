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
 *   message, a blank current message never replaces a recorded one, and a code blank in both
 *   takes the code itself as its message. Inputs are never mutated.
 * - A live-only code ({@link LIVE_ONLY_FLAG_CODES}: exactly `unverified_discrepancies`, whose
 *   count AAP 0.5.4.2 P1 reads from the overlay) is read from the current set alone: a recorded
 *   one stays, at its recorded place, only while the overlay carries it, and its message is the
 *   current one, else the code, never the frozen recorded text. A card recorded with one open
 *   discrepancy therefore drops the flag once none is open.
 * - {@link FLAG_LABELS} gives every flag code the label AAP 0.5.5 names on the flags row, and is
 *   frozen.
 * - {@link flagText} puts the code's label before a message that does not name it ("Stale: Data
 *   as of …", "Short window: Window of 10 days …", "Meeting time proxy: 1 other meeting …"),
 *   shows a message that names it, in the singular or the plural, as written ("Simulated data
 *   (Google)", "Not yet distinguishable …", "Unverified while 1 discrepancy is open"), shows the
 *   label alone for a blank message or the code itself, and the message alone, else the code,
 *   for a code it has no label for.
 * - `FlagList` renders a list named "Flags" with one badge per merged code, showing
 *   {@link flagText}, and renders nothing when no flag applies.
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
import { byTextContent } from '../../test/text';
import {
  FLAG_LABELS,
  FlagList,
  LIVE_ONLY_FLAG_CODES,
  WARNING_FLAG_CODES,
  flagText,
  flagTone,
  mergeFlags,
} from './FlagList';

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

/** The label AAP 0.5.5 names on the flags row for each code. */
const AAP_LABELS: Readonly<Record<FlagCode, string>> = {
  simulated: 'Simulated data',
  stale: 'Stale',
  incomplete: 'Incomplete',
  short_window: 'Short window',
  unverified_discrepancies: 'Unverified while discrepancies open',
  attribution_floor: 'Floor',
  creative_floor: 'Floor',
  deal_floor: 'Floor',
  unknown_crm_evidence: 'Unknown CRM evidence',
  meeting_time_proxy: 'Meeting time proxy',
  pipeline_unavailable: 'Pipeline unavailable',
  no_crm_link: 'No CRM link',
  not_distinguishable: 'Not distinguishable',
  search_term_coverage: 'Low search-term coverage',
  shared_budget: 'Shared budget',
};

/** The stale fixture flag's badge text: its message does not say "stale", so the label leads. */
const STALE_BADGE = `Stale: ${FIXTURE_FLAGS.stale.message}`;

/** The short-window fixture flag's badge text. */
const SHORT_WINDOW_BADGE = 'Short window: Window of 10 days is below the 14-day trusted minimum';

/** The meeting-time-proxy fixture flag's badge text ("proxied" is not the word "proxy"). */
const MEETING_PROXY_BADGE =
  'Meeting time proxy: 1 other meeting (ordering unverified): booking time is proxied by ' +
  'record creation, so it is excluded from cost per meeting';

/** The unknown-CRM-evidence fixture flag's badge text: its message names "unknown" after "CRM". */
const UNKNOWN_EVIDENCE_BADGE =
  'Unknown CRM evidence: Floor: CRM evidence unknown for 10% of matched contacts (2 of 20)';

/**
 * The badge text of each {@link FIXTURE_FLAGS} flag: the message as written where it names its
 * label, else the label, a colon and the message.
 */
const FIXTURE_BADGE_TEXTS: Readonly<Record<FlagCode, string>> = {
  simulated: 'Simulated data',
  stale: STALE_BADGE,
  incomplete: FIXTURE_FLAGS.incomplete.message,
  short_window: SHORT_WINDOW_BADGE,
  unverified_discrepancies: FIXTURE_FLAGS.unverified_discrepancies.message,
  attribution_floor: 'Floor: 12% of leads unmatched in HubSpot',
  creative_floor: 'Per-creative breakdown is a floor: 12.5% of leads have no creative',
  deal_floor: 'Deal counts are a floor: orphan-deal share 25% (1 of 4 deals without a contact)',
  unknown_crm_evidence: UNKNOWN_EVIDENCE_BADGE,
  meeting_time_proxy: MEETING_PROXY_BADGE,
  pipeline_unavailable: FIXTURE_FLAGS.pipeline_unavailable.message,
  no_crm_link: FIXTURE_FLAGS.no_crm_link.message,
  not_distinguishable: 'Not yet distinguishable (adjusted p = 0.21)',
  search_term_coverage: 'Low search-term coverage: 74% of keyword spend',
  shared_budget: 'Shared budget across 2 campaigns: decided at budget level',
};

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

describe('FLAG_LABELS', () => {
  it('labels every flag code, and nothing else', () => {
    // FIXTURE_FLAG_CODES are the keys of FIXTURE_FLAGS, a Record over the FlagCode union.
    expect(Object.keys(FLAG_LABELS).sort()).toEqual([...FIXTURE_FLAG_CODES].sort());
    expect(Object.keys(FLAG_LABELS).sort()).toEqual(Object.keys(FIXTURE_FLAGS).sort());
  });

  it.each(FIXTURE_FLAG_CODES)('labels %s as AAP 0.5.5 names it on the flags row', (code) => {
    expect(FLAG_LABELS[code]).toBe(AAP_LABELS[code]);
    expect(FLAG_LABELS[code].trim()).not.toBe('');
  });

  it('gives the attribution, creative and deal floors the one "Floor" label', () => {
    expect([
      FLAG_LABELS.attribution_floor,
      FLAG_LABELS.creative_floor,
      FLAG_LABELS.deal_floor,
    ]).toEqual(['Floor', 'Floor', 'Floor']);
  });

  it('is frozen, so no caller can relabel a code', () => {
    expect(Object.isFrozen(FLAG_LABELS)).toBe(true);
    expect(() => {
      (FLAG_LABELS as Record<FlagCode, string>).stale = 'Old';
    }).toThrow(TypeError);
    expect(FLAG_LABELS.stale).toBe('Stale');
  });
});

describe('flagText', () => {
  it.each<[FlagCode, string, string]>([
    ['stale', FIXTURE_FLAGS.stale.message, STALE_BADGE],
    ['short_window', FIXTURE_FLAGS.short_window.message, SHORT_WINDOW_BADGE],
    [
      'short_window',
      'Window of 6 days is below the 14-day trusted minimum',
      'Short window: Window of 6 days is below the 14-day trusted minimum',
    ],
    ['meeting_time_proxy', FIXTURE_FLAGS.meeting_time_proxy.message, MEETING_PROXY_BADGE],
    [
      'meeting_time_proxy',
      '6 other meetings (ordering unverified): booking time is proxied by record creation, so ' +
        'they are excluded from cost per meeting',
      'Meeting time proxy: 6 other meetings (ordering unverified): booking time is proxied by ' +
        'record creation, so they are excluded from cost per meeting',
    ],
    ['unknown_crm_evidence', FIXTURE_FLAGS.unknown_crm_evidence.message, UNKNOWN_EVIDENCE_BADGE],
    [
      'pipeline_unavailable',
      'Qualified Lead count unavailable: r',
      'Pipeline unavailable: Qualified Lead count unavailable: r',
    ],
  ])('puts the %s label before a message that does not name it: "%s"', (code, message, text) => {
    expect(flagText({ code, message })).toBe(text);
  });

  it.each<[FlagCode, string]>([
    ['simulated', 'Simulated data (Google)'],
    ['simulated', 'Joins live and simulated data (HubSpot simulated)'],
    ['incomplete', 'Incomplete: x'],
    ['incomplete', 'Baseline incomplete: x'],
    ['unverified_discrepancies', 'Unverified while 3 discrepancies are open'],
    ['unverified_discrepancies', 'Unverified while 1 discrepancy is open'],
    ['attribution_floor', 'Floor: 12% of leads unmatched in HubSpot'],
    ['creative_floor', FIXTURE_FLAGS.creative_floor.message],
    ['deal_floor', FIXTURE_FLAGS.deal_floor.message],
    ['not_distinguishable', 'Not yet distinguishable (adjusted p = 0.21)'],
    ['unknown_crm_evidence', 'Unknown CRM evidence for 3 of 25 eligible contacts'],
    ['pipeline_unavailable', 'Pipeline unavailable: r'],
    ['no_crm_link', 'No CRM link for Google campaigns: r'],
    ['search_term_coverage', 'Low search-term coverage: 62% of keyword spend'],
    ['shared_budget', 'Shared budget across 2 campaigns: decided at budget level'],
  ])('shows a %s message that already names its label as written: "%s"', (code, message) => {
    expect(flagText({ code, message })).toBe(message);
  });

  it('gives each fixture flag its badge text', () => {
    expect(FIXTURE_FLAG_CODES.map((code) => flagText(FIXTURE_FLAGS[code]))).toEqual(
      FIXTURE_FLAG_CODES.map((code) => FIXTURE_BADGE_TEXTS[code]),
    );
  });

  it('matches the label words in order, ignoring case and punctuation, never inside a word', () => {
    expect(flagText({ code: 'stale', message: 'STALE since 05:31' })).toBe('STALE since 05:31');
    expect(flagText({ code: 'short_window', message: 'Short-window run' })).toBe(
      'Short-window run',
    );
    expect(flagText({ code: 'short_window', message: 'Window is short' })).toBe(
      'Short window: Window is short',
    );
    expect(flagText({ code: 'stale', message: 'Stalemate' })).toBe('Stale: Stalemate');
  });

  it('trims the message it shows', () => {
    expect(flagText({ code: 'simulated', message: '  Simulated data (Google)\n' })).toBe(
      'Simulated data (Google)',
    );
    expect(flagText({ code: 'stale', message: '  Data as of 05:31  ' })).toBe(
      'Stale: Data as of 05:31',
    );
  });

  it.each(FIXTURE_FLAG_CODES)(
    'shows the %s label alone for a blank message or the code itself',
    (code) => {
      for (const message of ['', '   ', code, ` ${code} `]) {
        expect(flagText({ code, message })).toBe(AAP_LABELS[code]);
      }
    },
  );

  it('shows the label alone when the message is not a string at run time', () => {
    expect(flagText({ code: 'stale', message: null as unknown as string })).toBe('Stale');
  });

  it('shows the message alone, else the code, for a code a later API version adds', () => {
    const code = 'budget_paced' as FlagCode;

    expect(flagText({ code, message: ' Paced at 80% of the daily budget ' })).toBe(
      'Paced at 80% of the daily budget',
    );
    expect(flagText({ code, message: '  ' })).toBe('budget_paced');
    expect(flagText({ code, message: 'budget_paced' })).toBe('budget_paced');
  });

  it('never takes a label from the object prototype', () => {
    const code = 'constructor' as FlagCode;

    expect(flagText({ code, message: 'Built at run time' })).toBe('Built at run time');
    expect(flagText({ code, message: '' })).toBe('constructor');
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
          message: 'Unverified while 1 discrepancy is open',
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

  it('gives a code blank in both sets the code itself as its message, and its badge the label', () => {
    const recorded = [makeFlag('deal_floor', { message: '' })];
    const current = [makeFlag('deal_floor', { message: '   ' })];

    expect(mergeFlags(recorded, current)).toEqual([{ code: 'deal_floor', message: 'deal_floor' }]);

    render(<FlagList flags={recorded} current={current} />);

    expect(flagItems().map((entry) => entry.textContent)).toEqual(['Floor']);
  });

  it('defaults the current set to none and returns new objects without mutating its inputs', () => {
    const recorded: readonly Flag[] = Object.freeze([Object.freeze(makeFlag('simulated'))]);

    const merged = mergeFlags(recorded);

    expect(merged).toEqual([{ code: 'simulated', message: 'Simulated data' }]);
    expect(merged[0]).not.toBe(recorded[0]);
    expect(mergeFlags([], [])).toEqual([]);
  });

  it('reads exactly the unverified-discrepancies code from the current set alone', () => {
    expect([...LIVE_ONLY_FLAG_CODES]).toEqual(['unverified_discrepancies']);
  });

  it('drops a recorded unverified flag the current set no longer carries, keeping the rest', () => {
    // Recorded while 1 discrepancy was open; 0 are open now, so the overlay omits the flag.
    const recorded: readonly Flag[] = Object.freeze([
      Object.freeze(makeFlag('simulated')),
      Object.freeze(
        makeFlag('unverified_discrepancies', {
          message: 'Unverified while 1 discrepancy is open',
        }),
      ),
      Object.freeze(makeFlag('no_crm_link')),
    ]);
    const current: readonly Flag[] = Object.freeze([Object.freeze(makeFlag('stale'))]);

    expect(mergeFlags(recorded, current)).toEqual([
      { code: 'simulated', message: 'Simulated data' },
      { code: 'no_crm_link', message: FIXTURE_FLAGS.no_crm_link.message },
      { code: 'stale', message: FIXTURE_FLAGS.stale.message },
    ]);
    expect(mergeFlags(recorded)).toEqual([
      { code: 'simulated', message: 'Simulated data' },
      { code: 'no_crm_link', message: FIXTURE_FLAGS.no_crm_link.message },
    ]);
    expect(recorded).toHaveLength(3);
    expect(current).toHaveLength(1);
  });

  it('gives a live-only code the code itself as its message, never the frozen recorded text', () => {
    const recorded = [
      makeFlag('unverified_discrepancies', {
        message: 'Unverified while 1 discrepancy is open',
      }),
      makeFlag('simulated'),
    ];
    const current = [makeFlag('unverified_discrepancies', { message: '  ' })];

    expect(mergeFlags(recorded, current)).toEqual([
      { code: 'unverified_discrepancies', message: 'unverified_discrepancies' },
      { code: 'simulated', message: 'Simulated data' },
    ]);

    render(<FlagList flags={recorded} current={current} />);

    // The badge shows the code's label alone, and the frozen count nowhere.
    expect(flagItems().map((entry) => entry.textContent)).toEqual([
      'Unverified while discrepancies open',
      'Simulated data',
    ]);
    expect(screen.queryByText('Unverified while 1 discrepancy is open')).toBeNull();
  });
});

describe('FlagList', () => {
  it('renders one badge per code, showing its label and message, toned by the code', () => {
    const flags = FIXTURE_FLAG_CODES.map((code) => makeFlag(code));
    // A live-only code shows only while the overlay carries it, so the current set carries those.
    const current = FIXTURE_FLAG_CODES.filter((code) => LIVE_ONLY_FLAG_CODES.has(code)).map(
      (code) => makeFlag(code),
    );
    render(<FlagList flags={flags} current={current} />);

    const items = flagItems();
    expect(items.map((item) => item.textContent)).toEqual(
      FIXTURE_FLAG_CODES.map((code) => FIXTURE_BADGE_TEXTS[code]),
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
      'Unverified while 1 discrepancy is open',
    ]);
  });

  it('renders the current flags alone when the run recorded none', () => {
    render(<FlagList flags={[]} current={[makeFlag('stale')]} />);

    expect(flagItems().map((entry) => entry.textContent)).toEqual([STALE_BADGE]);
    expect(screen.getByText(byTextContent('.ds-badge', STALE_BADGE)).className).toBe(
      'ds-badge ds-badge--warning',
    );
  });

  it('names the Stale, Short window and Meeting time proxy labels before their messages', () => {
    render(
      <FlagList
        flags={[makeFlag('short_window'), makeFlag('meeting_time_proxy')]}
        current={[makeFlag('stale')]}
      />,
    );

    expect(flagItems().map((entry) => entry.textContent)).toEqual([
      'Short window: Window of 10 days is below the 14-day trusted minimum',
      'Meeting time proxy: 1 other meeting (ordering unverified): booking time is proxied by ' +
        'record creation, so it is excluded from cost per meeting',
      STALE_BADGE,
    ]);
    expect(screen.getByText(SHORT_WINDOW_BADGE).className).toBe('ds-badge ds-badge--warning');
    expect(screen.getByText(MEETING_PROXY_BADGE).className).toBe('ds-badge ds-badge--info');
    expect(screen.getByText(byTextContent('.ds-badge', STALE_BADGE)).className).toBe(
      'ds-badge ds-badge--warning',
    );
  });

  it('renders nothing, not even an empty list, when no flag applies', () => {
    const { container } = render(<FlagList flags={[]} />);

    expect(container.childElementCount).toBe(0);
    expect(screen.queryByRole('list', { name: 'Flags' })).toBeNull();
  });

  it('drops the unverified badge once the overlay reports no open discrepancy', () => {
    const recorded = [
      makeFlag('simulated'),
      makeFlag('unverified_discrepancies', {
        message: 'Unverified while 1 discrepancy is open',
      }),
      makeFlag('no_crm_link'),
    ];
    const { rerender } = render(
      <FlagList
        flags={recorded}
        current={[
          makeFlag('unverified_discrepancies', {
            message: 'Unverified while 1 discrepancy is open',
          }),
        ]}
      />,
    );

    expect(flagItems().map((entry) => entry.textContent)).toEqual([
      'Simulated data',
      'Unverified while 1 discrepancy is open',
      FIXTURE_FLAGS.no_crm_link.message,
    ]);

    // The discrepancy is resolved: 0 open, and the overlay no longer carries the flag.
    rerender(<FlagList flags={recorded} current={[makeFlag('simulated')]} />);

    expect(flagItems().map((entry) => entry.textContent)).toEqual([
      'Simulated data',
      FIXTURE_FLAGS.no_crm_link.message,
    ]);
    expect(screen.queryByText(/^Unverified while /u)).toBeNull();
  });

  it('renders nothing when the only recorded flag is a live-only code the overlay omits', () => {
    const { container } = render(
      <FlagList flags={[makeFlag('unverified_discrepancies')]} current={[]} />,
    );

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
      message: 'Unverified while 1 discrepancy is open',
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
    expect(screen.queryByText('Unverified while 1 discrepancy is open')).toBeNull();
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
