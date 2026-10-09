/**
 * Component tests for `SectionFrame` and its helpers, the behaviour the five `/actions` sections
 * share (AAP 0.5.5 `/actions` layout, "Placement of items that must be resolved first";
 * AAP 0.5.4.2 P1, P6 and feedback suppression).
 *
 * They hold the module to its TSDoc contract:
 * - anchors: `section-{key}`, the sanitised `group-{sectionKey}-{groupKey}` and
 *   {@link DISCREPANCY_GROUP_ANCHOR}, the target of the discrepancy banner and held cards;
 * - item state: held by the run or by the live overlay, hidden by feedback since the run, and
 *   the partition of a group into visible, held and hidden items in API order;
 * - the held disclosure: collapsed on first render, a real button with `aria-expanded` and
 *   `aria-controls`, nothing at all for a count that is not positive;
 * - labelled fields, suppression labels and finding tones;
 * - the frame itself: the section region and its `<h2>`, findings first as banners that are never
 *   alerts, groups in API order as anchored `<h3>` regions, the `renderGroup`, `renderGroups` and
 *   `renderEmptyGroup` hooks, the empty state and its reason lines, the withheld list and the
 *   "Not shown" counts.
 *
 * The frame fetches nothing and never renders a card itself, so every test passes a plain
 * `renderItem` and renders without providers or a network stub.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import type { FindingCode, Group, Item, Section } from '../../../api/types';
import {
  makeFinding,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeSection,
  makeWithheld,
} from '../../../test/fixtures';
import { withheldLine } from '../WithheldList';
import {
  DISCREPANCY_GROUP_ANCHOR,
  FieldList,
  HeldDisclosure,
  SUPPRESSION_LABELS,
  SectionFrame,
  findingTone,
  groupAnchorId,
  isHeld,
  isHidden,
  partitionGroup,
  sectionAnchorId,
  suppressionLabel,
} from './SectionFrame';
import type { GroupParts, SectionFrameProps } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------------------------

/** A visible item: not held by the run or the overlay, not hidden by feedback. */
function visibleItem(id: number, action: string): Item {
  return makeItem({ id, rank: id, action });
}

/** An item the run computed as held until discrepancies are resolved (P1). */
function heldItem(id: number, action: string): Item {
  return makeItem({ id, rank: id, action, held: true, held_reason: 'Resolve discrepancy first' });
}

/** An item the live overlay holds, for a discrepancy detected after the run. */
function heldNowItem(id: number, action: string): Item {
  return makeItem({
    id,
    rank: id,
    action,
    current: makeItemOverlay({ held_now: true, held_reason_now: 'Resolve discrepancy first' }),
  });
}

/** An item feedback given since the run hides now. */
function hiddenItem(id: number, action: string, held = false): Item {
  return makeItem({
    id,
    rank: id,
    action,
    held,
    current: makeItemOverlay({ hidden_by: 'dismiss' }),
  });
}

/** The `renderItem` every frame test passes: a plain line naming the group and the action. */
function renderLine(item: Item, group: Group): ReactNode {
  return <div data-testid={`item-${String(item.id)}`}>{`${group.key}: ${item.action}`}</div>;
}

/** Renders the frame with {@link renderLine} unless the test passes its own `renderItem`. */
function renderFrame(props: Partial<SectionFrameProps> & { section: Section }) {
  return render(<SectionFrame renderItem={renderLine} {...props} />);
}

/** The text of every `<li>` of the list with this accessible name. */
function listLines(name: string): string[] {
  const list = screen.getByRole('list', { name });
  return within(list)
    .getAllByRole('listitem')
    .map((entry) => entry.textContent);
}

/** Whether `later` comes after `earlier` in document order. */
function follows(earlier: Element, later: Element): boolean {
  return (earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

// ---------------------------------------------------------------------------------------------
// Anchors
// ---------------------------------------------------------------------------------------------

describe('anchors', () => {
  it('names a section region section-{key}', () => {
    expect(sectionAnchorId('keywords')).toBe('section-keywords');
    expect(sectionAnchorId('white_papers')).toBe('section-white_papers');
  });

  it('sanitises dynamic group keys into link-safe ids', () => {
    expect(groupAnchorId('creative', 'campaign:123')).toBe('group-creative-campaign-123');
    expect(groupAnchorId('white_papers', 'cohort:EUR')).toBe('group-white_papers-cohort-EUR');
    expect(groupAnchorId('keywords', 'block list/2')).toBe('group-keywords-block-list-2');
  });

  it('replaces a character outside the Basic Multilingual Plane with a single dash', () => {
    expect(groupAnchorId('creative', 'a\u{1F600}b')).toBe('group-creative-a-b');
  });

  it('derives the discrepancy group anchor the banner and held cards link to', () => {
    expect(DISCREPANCY_GROUP_ANCHOR).toBe('group-campaigns-discrepancies');
    expect(groupAnchorId('campaigns', 'discrepancies')).toBe(DISCREPANCY_GROUP_ANCHOR);
  });
});

// ---------------------------------------------------------------------------------------------
// Item state
// ---------------------------------------------------------------------------------------------

describe('item state', () => {
  it('treats an item as held when the run held it or the overlay holds it now', () => {
    expect(isHeld(visibleItem(1, 'Pause'))).toBe(false);
    expect(isHeld(heldItem(2, 'Pause'))).toBe(true);
    expect(isHeld(heldNowItem(3, 'Pause'))).toBe(true);
  });

  it('treats an item as hidden only while current.hidden_by is set', () => {
    expect(isHidden(visibleItem(1, 'Pause'))).toBe(false);
    expect(isHidden(hiddenItem(2, 'Pause'))).toBe(true);
    for (const kind of ['dismiss', 'snooze', 'done'] as const) {
      expect(isHidden(makeItem({ current: makeItemOverlay({ hidden_by: kind }) }))).toBe(true);
    }
  });

  it('partitions a group into visible, held and hidden items, keeping the API order', () => {
    const group = makeGroup({
      key: 'pause',
      title: 'Pause',
      items: [
        heldItem(1, 'held first'),
        visibleItem(2, 'visible first'),
        hiddenItem(3, 'hidden'),
        heldNowItem(4, 'held now'),
        hiddenItem(5, 'hidden and held', true),
        visibleItem(6, 'visible second'),
      ],
    });

    const { visible, held, hiddenCount } = partitionGroup(group);

    expect(visible.map((item) => item.id)).toEqual([2, 6]);
    expect(held.map((item) => item.id)).toEqual([1, 4]);
    // A hidden item is counted whether or not it is also held.
    expect(hiddenCount).toBe(2);
  });

  it('partitions an empty group into nothing', () => {
    expect(partitionGroup(makeGroup())).toEqual({ visible: [], held: [], hiddenCount: 0 });
  });
});

// ---------------------------------------------------------------------------------------------
// HeldDisclosure
// ---------------------------------------------------------------------------------------------

describe('HeldDisclosure', () => {
  it('starts collapsed behind a button that names the count and controls the region', () => {
    render(
      <HeldDisclosure id="group-campaigns-pause-held" count={2}>
        <span>Held card A</span>
        <span>Held card B</span>
      </HeldDisclosure>,
    );

    const toggle = screen.getByRole('button', {
      name: 'Held until discrepancies are resolved (2)',
    });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe('group-campaigns-pause-held');

    const region = document.getElementById('group-campaigns-pause-held');
    expect(region).not.toBeNull();
    expect(region?.hidden).toBe(true);
    expect(region?.textContent).toBe('Held card AHeld card B');
  });

  it('expands and collapses again when the button is operated', () => {
    render(
      <HeldDisclosure id="held-region" count={1}>
        <span>Held card</span>
      </HeldDisclosure>,
    );
    const toggle = screen.getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    const region = document.getElementById('held-region');

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(region?.hidden).toBe(false);

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(region?.hidden).toBe(true);
  });

  it('formats a large count with grouping', () => {
    render(
      <HeldDisclosure id="many-held" count={1234}>
        <span>Held card</span>
      </HeldDisclosure>,
    );
    expect(
      screen.getByRole('button', { name: 'Held until discrepancies are resolved (1,234)' }),
    ).toBeTruthy();
  });

  it.each([
    ['zero', 0],
    ['a negative count', -1],
    ['NaN', Number.NaN],
  ])('renders nothing for %s', (_label, count) => {
    const { container } = render(
      <HeldDisclosure id="nothing-held" count={count}>
        <span>Held card</span>
      </HeldDisclosure>,
    );
    expect(container.childElementCount).toBe(0);
    expect(screen.queryByRole('button')).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// FieldList
// ---------------------------------------------------------------------------------------------

describe('FieldList', () => {
  it('lists each field as "{label}: {value}" in the order given, named by its label', () => {
    render(
      <FieldList
        label="Keyword details"
        fields={[
          { key: 'term', label: 'Term', value: 'crm software' },
          { key: 'spend', label: 'Spend', value: '$412.30' },
          { key: 'conversions', label: 'Conversions', value: 0 },
        ]}
      />,
    );
    expect(listLines('Keyword details')).toEqual([
      'Term: crm software',
      'Spend: $412.30',
      'Conversions: 0',
    ]);
  });

  it('renders "unavailable" for a value React would render as nothing', () => {
    render(
      <FieldList
        label="Missing values"
        fields={[
          { key: 'null', label: 'Null', value: null },
          { key: 'undefined', label: 'Undefined', value: undefined },
          { key: 'false', label: 'False', value: false },
          { key: 'true', label: 'True', value: true },
          { key: 'empty', label: 'Empty', value: '' },
        ]}
      />,
    );
    expect(listLines('Missing values')).toEqual([
      'Null: unavailable',
      'Undefined: unavailable',
      'False: unavailable',
      'True: unavailable',
      'Empty: unavailable',
    ]);
  });

  it('keeps duplicate keys apart and leaves the list unnamed without a label', () => {
    render(
      <FieldList
        fields={[
          { key: 'spend', label: 'Spend', value: '$1.00' },
          { key: 'spend', label: 'Spend', value: '$2.00' },
        ]}
      />,
    );
    const list = screen.getByRole('list');
    expect(list.getAttribute('aria-label')).toBeNull();
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent),
    ).toEqual(['Spend: $1.00', 'Spend: $2.00']);
  });

  it('renders nothing without fields', () => {
    const { container } = render(<FieldList label="Nothing" fields={[]} />);
    expect(container.childElementCount).toBe(0);
  });
});

// ---------------------------------------------------------------------------------------------
// Suppression labels and finding tones
// ---------------------------------------------------------------------------------------------

describe('suppression labels', () => {
  it('labels every reason the runner writes to run_suppression', () => {
    expect(SUPPRESSION_LABELS).toEqual({
      feedback_dismiss: 'dismissed',
      feedback_snooze: 'snoozed',
      feedback_done: 'marked done',
      g1_blocked: 'spend increases blocked by the high-utilization guard',
      tracking_absent: 'suppressed until tracking is verified',
    });
    expect(Object.isFrozen(SUPPRESSION_LABELS)).toBe(true);
    expect(suppressionLabel('feedback_snooze')).toBe('snoozed');
  });

  it('shows an unknown reason as its raw key, never reading the object prototype', () => {
    expect(suppressionLabel('p9_future_reason')).toBe('p9_future_reason');
    expect(suppressionLabel('constructor')).toBe('constructor');
    expect(suppressionLabel('toString')).toBe('toString');
  });
});

describe('findingTone', () => {
  it.each<FindingCode>([
    'conversion_tracking_absent',
    'incomplete_dataset',
    'pipeline_unavailable',
    'segment_window_unavailable',
    'k1_no_baseline',
  ])('tones %s as a warning', (code) => {
    expect(findingTone(code)).toBe('warning');
  });

  it.each<FindingCode>(['no_crm_link', 'search_term_coverage', 'skipped_stage'])(
    'tones %s as information',
    (code) => {
      expect(findingTone(code)).toBe('info');
    },
  );
});

// ---------------------------------------------------------------------------------------------
// SectionFrame
// ---------------------------------------------------------------------------------------------

describe('SectionFrame', () => {
  it('renders the section region anchored at section-{key} and named by its <h2> title', () => {
    renderFrame({ section: makeSection('budget') });

    const region = screen.getByRole('region', { name: 'Budget changes' });
    expect(region.id).toBe('section-budget');
    expect(region.tagName).toBe('SECTION');
    const heading = within(region).getByRole('heading', { level: 2, name: 'Budget changes' });
    expect(region.getAttribute('aria-labelledby')).toBe(heading.id);
  });

  it('falls back to the section key when the title is blank', () => {
    renderFrame({ section: makeSection('creative', { title: '   ' }) });
    expect(screen.getByRole('heading', { level: 2, name: 'creative' })).toBeTruthy();
  });

  it('falls back to the section key when an untyped payload sends no title string', () => {
    // A payload that bypassed the contract, as an older or newer API could send it.
    const untitled = { ...makeSection('white_papers'), title: null } as unknown as Section;
    renderFrame({ section: untitled });
    expect(screen.getByRole('heading', { level: 2, name: 'white_papers' })).toBeTruthy();
  });

  it('puts the findings first, in API order and toned by code, and never as alerts', () => {
    const tracking = makeFinding({
      code: 'conversion_tracking_absent',
      section: 'campaigns',
      message:
        'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
    });
    const crm = makeFinding({
      code: 'no_crm_link',
      section: 'campaigns',
      message: 'Google campaigns are not linked to the CRM.',
    });
    const blank = makeFinding({ code: 'incomplete_dataset', section: 'campaigns', message: ' ' });
    renderFrame({
      section: makeSection('campaigns', {
        findings: [tracking, crm, blank],
        groups: [
          makeGroup({ key: 'pause', title: 'Pause', items: [visibleItem(1, 'Pause campaign')] }),
        ],
      }),
    });

    const region = screen.getByRole('region', { name: 'Campaigns to pause or launch' });
    const titles = [...region.querySelectorAll('.ds-banner__title')].map(
      (node) => node.textContent,
    );
    // A blank message falls back to the finding code, so a banner is never empty.
    expect(titles).toEqual([tracking.message, crm.message, 'incomplete_dataset']);

    const banners = [...region.querySelectorAll('.ds-banner')];
    expect(banners.map((banner) => banner.className)).toEqual([
      'ds-banner ds-banner--warning',
      'ds-banner ds-banner--info',
      'ds-banner ds-banner--warning',
    ]);
    expect(screen.queryByRole('alert')).toBeNull();

    // Tracking findings sit at the top of the section, above its groups (AAP 0.5.5).
    const firstBanner = banners[0];
    const group = screen.getByRole('region', { name: 'Pause' });
    expect(firstBanner).toBeDefined();
    if (firstBanner !== undefined) {
      expect(follows(firstBanner, group)).toBe(true);
    }
  });

  it('shows a tracking and a coverage finding, in API order, above the first group heading', () => {
    const tracking = makeFinding({
      code: 'conversion_tracking_absent',
      message:
        'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
    });
    const coverage = makeFinding({ code: 'search_term_coverage' });
    renderFrame({
      section: makeSection('keywords', {
        findings: [tracking, coverage],
        groups: [
          makeGroup({ items: [visibleItem(1, 'Add as exact-match negative')] }),
          makeGroup({
            key: 'expand',
            title: 'Expansion candidates',
            items: [visibleItem(2, 'Add as phrase-match keyword')],
          }),
        ],
      }),
    });

    const trackingMessage = screen.getByText(tracking.message);
    const coverageMessage = screen.getByText(coverage.message);
    const firstHeading = screen.getAllByRole('heading', { level: 3 })[0];
    expect(firstHeading?.textContent).toBe('Block / negative-keyword candidates');
    if (firstHeading !== undefined) {
      expect(follows(trackingMessage, firstHeading)).toBe(true);
      expect(follows(coverageMessage, firstHeading)).toBe(true);
    }
    expect(follows(trackingMessage, coverageMessage)).toBe(true);
    // Section findings are banners, never alerts: `alert` belongs to the discrepancy banner.
    expect(screen.queryByRole('alert')).toBeNull();
    expect(findingTone(tracking.code)).toBe('warning');
    expect(findingTone(coverage.code)).toBe('info');
  });

  it('places beforeGroups after the findings and before the groups', () => {
    renderFrame({
      section: makeSection('keywords', {
        findings: [makeFinding()],
        groups: [makeGroup({ items: [visibleItem(1, 'Add as exact-match negative')] })],
      }),
      beforeGroups: <div data-testid="before-groups">Section note</div>,
    });

    const note = screen.getByTestId('before-groups');
    const banner = document.querySelector('.ds-banner');
    const group = screen.getByRole('region', { name: 'Block / negative-keyword candidates' });
    expect(banner).not.toBeNull();
    if (banner !== null) {
      expect(follows(banner, note)).toBe(true);
    }
    expect(follows(note, group)).toBe(true);
  });

  it('renders each group with a shown item as an anchored <h3> region, in API order', () => {
    renderFrame({
      section: makeSection('campaigns', {
        groups: [
          makeGroup({
            key: 'discrepancies',
            title: 'Discrepancies',
            items: [visibleItem(1, 'C0')],
          }),
          makeGroup({ key: 'pause', title: 'Pause', items: [visibleItem(2, 'C1')] }),
          makeGroup({ key: 'scale', title: 'Scale', items: [] }),
          makeGroup({ key: 'launch', title: ' ', items: [visibleItem(3, 'C3b')] }),
        ],
      }),
    });

    const section = screen.getByRole('region', { name: 'Campaigns to pause or launch' });
    // The empty Scale group is left out; a blank title falls back to the group key.
    expect(
      within(section)
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(['Discrepancies', 'Pause', 'launch']);

    const discrepancies = screen.getByRole('region', { name: 'Discrepancies' });
    expect(discrepancies.id).toBe(DISCREPANCY_GROUP_ANCHOR);
    expect(screen.getByRole('region', { name: 'Pause' }).id).toBe('group-campaigns-pause');
    expect(screen.getByRole('region', { name: 'launch' }).id).toBe('group-campaigns-launch');
    expect(document.getElementById('group-campaigns-scale')).toBeNull();

    // renderItem receives the item's own group.
    expect(within(discrepancies).getByTestId('item-1').textContent).toBe('discrepancies: C0');
  });

  it('shows visible cards in API order, then held cards collapsed at the end', () => {
    const renderItem = vi.fn(renderLine);
    renderFrame({
      section: makeSection('campaigns', {
        groups: [
          makeGroup({
            key: 'pause',
            title: 'Pause',
            items: [
              heldItem(11, 'Held by the run'),
              visibleItem(12, 'Pause first'),
              hiddenItem(13, 'Dismissed since the run'),
              heldNowItem(14, 'Held since the run'),
              visibleItem(15, 'Pause second'),
            ],
          }),
        ],
      }),
      renderItem,
    });

    const group = screen.getByRole('region', { name: 'Pause' });
    const lines = within(group)
      .getAllByTestId(/^item-/u)
      .map((line) => line.textContent);
    expect(lines).toEqual([
      'pause: Pause first',
      'pause: Pause second',
      'pause: Held by the run',
      'pause: Held since the run',
    ]);
    // The hidden card is counted, never rendered.
    expect(screen.queryByText('pause: Dismissed since the run')).toBeNull();
    expect(
      renderItem.mock.calls.map(([item]) => item.id).sort((left, right) => left - right),
    ).toEqual([11, 12, 14, 15]);

    const toggle = within(group).getByRole('button', {
      name: 'Held until discrepancies are resolved (2)',
    });
    const heldRegion = document.getElementById('group-campaigns-pause-held');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe('group-campaigns-pause-held');
    expect(heldRegion?.hidden).toBe(true);
    expect(heldRegion?.contains(screen.getByTestId('item-11'))).toBe(true);
    expect(heldRegion?.contains(screen.getByTestId('item-14'))).toBe(true);
    expect(heldRegion?.contains(screen.getByTestId('item-12'))).toBe(false);

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(heldRegion?.hidden).toBe(false);

    expect(listLines('Not shown')).toEqual(['1 hidden since this run']);
  });

  it('keeps a group whose only items are held, and shows no empty state', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [makeGroup({ key: 'changes', title: 'Changes', items: [heldItem(1, 'B1')] })],
      }),
    });
    expect(screen.getByRole('region', { name: 'Changes' }).id).toBe('group-budget-changes');
    expect(
      screen.getByRole('button', { name: 'Held until discrepancies are resolved (1)' }),
    ).toBeTruthy();
    expect(screen.queryByText('No actions in this section')).toBeNull();
  });

  it('leaves out a group whose items are all hidden by feedback, and counts them', () => {
    const snoozed = makeItem({
      id: 7,
      rank: 7,
      action: 'Snoozed since the run',
      current: makeItemOverlay({ hidden_by: 'snooze' }),
    });
    renderFrame({
      section: makeSection('campaigns', {
        groups: [
          makeGroup({ key: 'pause', title: 'Pause', items: [visibleItem(1, 'C1')] }),
          makeGroup({ key: 'launch', title: 'Launch ideas', items: [snoozed] }),
        ],
      }),
    });

    expect(
      screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(['Pause']);
    expect(screen.queryByRole('region', { name: 'Launch ideas' })).toBeNull();
    expect(document.getElementById('group-campaigns-launch')).toBeNull();
    // The snoozed card is counted, never rendered.
    expect(screen.queryByTestId('item-7')).toBeNull();
    expect(screen.queryByText('launch: Snoozed since the run')).toBeNull();
    expect(listLines('Not shown')).toEqual(['1 hidden since this run']);
    expect(screen.queryByText('No actions in this section')).toBeNull();
  });

  it('lists the run suppression counts under "Not shown" beside the shown cards', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [makeGroup({ key: 'changes', title: 'Changes', items: [visibleItem(1, 'B1')] })],
        suppressed: { feedback_dismiss: 2, g1_blocked: 1, unknown_reason: 4 },
      }),
    });

    // The keys are the runner's run_suppression reasons. A g1_blocked count of exactly 1 reads
    // in the singular ("1 spend increase blocked …"), as SectionFrame's singular label map
    // defines it, and an unknown reason keeps its raw key so its count is never dropped.
    expect(listLines('Not shown')).toEqual([
      '2 dismissed',
      '1 spend increase blocked by the high-utilization guard',
      '4 unknown_reason',
    ]);
    expect(screen.getByTestId('item-1').textContent).toBe('changes: B1');
    expect(screen.queryByText('No actions in this section')).toBeNull();
  });

  it('lets renderGroup replace the body of each group with shown items, with its parts', () => {
    const seen: GroupParts[] = [];
    renderFrame({
      section: makeSection('white_papers', {
        groups: [
          makeGroup({
            key: 'cohort:EUR',
            title: 'EUR',
            items: [visibleItem(1, 'Double down'), heldItem(2, 'Held row'), hiddenItem(3, 'Gone')],
          }),
          makeGroup({ key: 'cohort:USD', title: 'USD', items: [] }),
        ],
      }),
      renderGroup: (group, parts) => {
        seen.push(parts);
        return (
          <div data-testid={`custom-${group.key}`}>
            {`custom body for ${group.title}`}
            {parts.body}
          </div>
        );
      },
    });

    // Called only for the group with something to show.
    expect(seen).toHaveLength(1);
    const parts = seen[0];
    expect(parts?.group.key).toBe('cohort:EUR');
    expect(parts?.anchorId).toBe('group-white_papers-cohort-EUR');
    expect(parts?.visible.map((item) => item.id)).toEqual([1]);
    expect(parts?.held.map((item) => item.id)).toEqual([2]);
    expect(parts?.hiddenCount).toBe(1);

    const region = screen.getByRole('region', { name: 'EUR' });
    expect(region.id).toBe('group-white_papers-cohort-EUR');
    expect(within(region).getByTestId('custom-cohort:EUR').textContent).toContain(
      'custom body for EUR',
    );
    // The override wrapped the default body, which still renders the cards and the disclosure.
    expect(within(region).getByTestId('item-1').textContent).toBe('cohort:EUR: Double down');
    expect(
      within(region).getByRole('button', { name: 'Held until discrepancies are resolved (1)' }),
    ).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'USD' })).toBeNull();
  });

  it('lets renderGroups replace the whole groups area, with the parts of every group', () => {
    const renderGroups = vi.fn((parts: readonly GroupParts[]) => (
      <div data-testid="tabs">
        {parts.map((part) => (
          <div key={part.anchorId} id={part.anchorId}>
            {`${part.group.title} (${String(part.visible.length)})`}
          </div>
        ))}
      </div>
    ));
    const renderGroup = vi.fn(() => <div>never</div>);
    renderFrame({
      section: makeSection('keywords', {
        groups: [
          makeGroup({ key: 'block', title: 'Block', items: [visibleItem(1, 'K1')] }),
          makeGroup({ key: 'expand', title: 'Expand', items: [] }),
          makeGroup({ key: 'review', title: 'Review', items: [heldItem(2, 'K3')] }),
        ],
      }),
      renderGroups,
      renderGroup,
    });

    expect(renderGroups).toHaveBeenCalledTimes(1);
    const parts = renderGroups.mock.calls[0]?.[0] ?? [];
    expect(parts.map((part) => part.group.key)).toEqual(['block', 'expand', 'review']);
    expect(parts.map((part) => part.anchorId)).toEqual([
      'group-keywords-block',
      'group-keywords-expand',
      'group-keywords-review',
    ]);
    expect(parts.map((part) => part.held.length)).toEqual([0, 0, 1]);

    expect(screen.getByTestId('tabs').textContent).toBe('Block (1)Expand (0)Review (0)');
    // The frame renders no group region of its own and ignores renderGroup.
    expect(screen.queryAllByRole('heading', { level: 3 })).toHaveLength(0);
    expect(renderGroup).not.toHaveBeenCalled();
    expect(screen.queryByText('No actions in this section')).toBeNull();
  });

  it('does not call renderGroups when nothing is shown, and shows the empty state instead', () => {
    const renderGroups = vi.fn(() => <div data-testid="tabs">tabs</div>);
    renderFrame({
      section: makeSection('keywords', {
        groups: [makeGroup({ key: 'block', title: 'Block', items: [hiddenItem(1, 'K1')] })],
      }),
      renderGroups,
    });

    expect(renderGroups).not.toHaveBeenCalled();
    expect(screen.queryByTestId('tabs')).toBeNull();
    expect(screen.getByText('No actions in this section')).toBeTruthy();
    expect(
      screen.getByText('1 hidden by feedback since this run (dismissed, snoozed or done)'),
    ).toBeTruthy();
  });

  it('keeps an empty group region, with its anchor, when renderEmptyGroup returns content', () => {
    const renderEmptyGroup = vi.fn((group: Group, parts: GroupParts) =>
      group.key === 'discrepancies' ? (
        <span>{`notice for ${parts.anchorId}`}</span>
      ) : group.key === 'scale' ? (
        ''
      ) : group.key === 'launch' ? (
        false
      ) : null,
    );
    renderFrame({
      section: makeSection('campaigns', {
        groups: [
          makeGroup({ key: 'discrepancies', title: 'Discrepancies', items: [] }),
          makeGroup({ key: 'pause', title: 'Pause', items: [visibleItem(1, 'C1')] }),
          makeGroup({ key: 'scale', title: 'Scale', items: [] }),
          makeGroup({ key: 'launch', title: 'Launch ideas', items: [hiddenItem(2, 'C3b')] }),
        ],
      }),
      renderEmptyGroup,
    });

    const discrepancies = screen.getByRole('region', { name: 'Discrepancies' });
    expect(discrepancies.id).toBe(DISCREPANCY_GROUP_ANCHOR);
    expect(discrepancies.textContent).toContain(`notice for ${DISCREPANCY_GROUP_ANCHOR}`);
    // Only the groups without a shown item are asked; an empty string or false leaves them out.
    expect(renderEmptyGroup.mock.calls.map(([group]) => group.key)).toEqual([
      'discrepancies',
      'scale',
      'launch',
    ]);
    expect(screen.queryByRole('region', { name: 'Scale' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Launch ideas' })).toBeNull();
  });

  describe('empty state', () => {
    it('says no rule fired when the section has no card, count, withheld line or finding', () => {
      renderFrame({
        section: makeSection('creative', {
          groups: [makeGroup({ key: 'campaign:1', title: 'Campaign', items: [] })],
          // Zero, negative and non-finite counts are not reported.
          suppressed: { feedback_dismiss: 0, g1_blocked: -2, tracking_absent: Number.NaN },
        }),
      });

      const region = screen.getByRole('region', { name: 'Copy/creative changes' });
      const empty = region.querySelector('.ds-emptystate');
      expect(empty?.querySelector('.ds-emptystate__title')?.textContent).toBe(
        'No actions in this section',
      );
      expect(empty?.querySelector('.ds-emptystate__reason')?.textContent).toBe(
        'No rule fired for this window',
      );
      expect(screen.queryByRole('list', { name: 'Not shown' })).toBeNull();
      expect(screen.queryByText('Withheld for sample size')).toBeNull();
    });

    it('quotes the withheld line as its reason, and the full withheld list follows', () => {
      const withheld = makeWithheld();
      renderFrame({
        section: makeSection('keywords', {
          groups: [makeGroup({ items: [] })],
          withheld: [withheld],
        }),
      });

      const empty = document.querySelector('.ds-emptystate');
      expect(empty).not.toBeNull();
      expect(empty?.querySelector('.ds-emptystate__title')?.textContent).toBe(
        'No actions in this section',
      );
      expect(empty?.querySelector('.ds-emptystate__reason')?.textContent).toBe(
        withheldLine(withheld),
      );
      expect(screen.queryByText('No rule fired for this window')).toBeNull();

      // The WithheldList still renders in full, below the empty state.
      const list = screen.getByRole('list', { name: 'Withheld for sample size' });
      expect(listLines('Withheld for sample size')).toEqual([withheldLine(withheld)]);
      if (empty !== null) {
        expect(follows(empty, list)).toBe(true);
      }
      expect(screen.queryByRole('list', { name: 'Not shown' })).toBeNull();
    });

    it('states why: hidden cards, suppressions, three withheld lines, findings', () => {
      const withheld = [
        makeWithheld(),
        makeWithheld({
          rule_id: 'K3',
          entity: { source: 'google', type: 'keyword', id: '81', name: 'crm software' },
          reason: 'quality-score days 3 of 5 required',
          sample: { observed: 3, required: 5, unit: 'days' },
        }),
        makeWithheld({
          rule_id: 'K1',
          entity: null,
          reason: 'fewer than 10 baseline conversions',
          sample: { observed: 9, required: 10, unit: 'conversions' },
        }),
        makeWithheld({
          rule_id: 'K2',
          entity: { source: 'google', type: 'search_term', id: '9', name: 'fourth term' },
          reason: 'conversions 2 of 3 required',
          sample: { observed: 2, required: 3, unit: 'conversions' },
        }),
      ];
      const finding = makeFinding({
        code: 'k1_no_baseline',
        message: 'K1 is withheld account-wide: the baseline has too few conversions.',
      });
      const blankFinding = makeFinding({ code: 'skipped_stage', message: '  ' });
      renderFrame({
        section: makeSection('keywords', {
          groups: [
            makeGroup({
              key: 'block',
              title: 'Block',
              items: [hiddenItem(1, 'K1'), hiddenItem(2, 'K1')],
            }),
            makeGroup({ key: 'review', title: 'Review', items: [hiddenItem(3, 'K3', true)] }),
          ],
          findings: [finding, blankFinding],
          withheld,
          suppressed: { feedback_snooze: 2, g1_blocked: 1, unknown_reason: 4, feedback_done: 0 },
        }),
      });

      const reason = document.querySelector('.ds-emptystate__reason');
      expect([...(reason?.querySelectorAll('span') ?? [])].map((line) => line.textContent)).toEqual(
        [
          '3 hidden by feedback since this run (dismissed, snoozed or done)',
          '2 snoozed',
          '1 spend increase blocked by the high-utilization guard',
          '4 unknown_reason',
          'K1 · crm pricing comparison: clicks 30 of 37 required — 30 / 37 clicks',
          'K3 · crm software: quality-score days 3 of 5 required — 3 / 5 days',
          'K1 · Account-wide: fewer than 10 baseline conversions — 9 / 10 conversions',
          'K1 is withheld account-wide: the baseline has too few conversions.',
        ],
      );
      // Groups without a shown item are left out.
      expect(screen.queryAllByRole('heading', { level: 3 })).toHaveLength(0);

      // The full withheld list follows under the default title, all four lines.
      expect(screen.getByText('Withheld for sample size')).toBeTruthy();
      expect(listLines('Withheld for sample size')).toHaveLength(4);

      expect(listLines('Not shown')).toEqual([
        '2 snoozed',
        '1 spend increase blocked by the high-utilization guard',
        '4 unknown_reason',
        '3 hidden since this run',
      ]);
    });

    it('uses the plural guard label for a count other than one', () => {
      renderFrame({ section: makeSection('budget', { suppressed: { g1_blocked: 3 } }) });
      expect(listLines('Not shown')).toEqual([
        '3 spend increases blocked by the high-utilization guard',
      ]);
    });
  });

  it('titles the withheld list with withheldTitle and lists it after the groups', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [makeGroup({ key: 'changes', title: 'Changes', items: [visibleItem(1, 'B1')] })],
        withheld: [
          makeWithheld({
            rule_id: 'B4',
            entity: {
              source: 'google',
              type: 'campaign',
              id: '21000302',
              name: 'Generic CRM — US',
            },
            reason: '3 weekend days of 4 required',
            sample: { observed: 3, required: 4, unit: 'weekend days' },
          }),
        ],
      }),
      withheldTitle: 'Patterns withheld for sample size',
    });

    expect(listLines('Patterns withheld for sample size')).toEqual([
      'B4 · Generic CRM — US: 3 weekend days of 4 required — 3 / 4 weekend days',
    ]);
    expect(screen.queryByText('Withheld for sample size')).toBeNull();
    expect(
      follows(
        screen.getByRole('region', { name: 'Changes' }),
        screen.getByRole('list', { name: 'Patterns withheld for sample size' }),
      ),
    ).toBe(true);
    expect(screen.queryByRole('list', { name: 'Not shown' })).toBeNull();
  });
});
