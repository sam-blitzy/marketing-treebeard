/**
 * Component tests for `SectionFrame` and its helpers, the behaviour the five `/actions` sections
 * share (AAP 0.5.5 `/actions` layout, "Placement of items that must be resolved first";
 * AAP 0.5.4.2 P1, P6 and feedback suppression).
 *
 * They hold the module to its TSDoc contract:
 * - anchors: `section-{key}`, the sanitised `group-{sectionKey}-{groupKey}` and
 *   {@link DISCREPANCY_GROUP_ANCHOR}, the target of the discrepancy banner and held cards;
 * - item state: held by the run or by the live overlay, computed from replaced data (historical,
 *   never held), hidden by feedback since the run, and the partition of a group into visible,
 *   historical, held and hidden items in API order;
 * - the held disclosure: collapsed on first render, a real button with `aria-expanded` and
 *   `aria-controls` over a region that always exists but mounts its content only from the first
 *   expansion on, nothing at all for a count that is not positive; a hash naming a held card
 *   expands it and scrolls to the card, and a hash change (a link followed) also focuses the
 *   card, once, while a page loaded at the hash moves no focus;
 * - the replaced-data block: open, named "Computed from replaced data ({count})" like the
 *   historical card badge, between the visible cards and the held disclosure;
 * - labelled fields, suppression labels and finding tones;
 * - the frame itself: the section region and its `<h2>`, findings first as banners that are never
 *   alerts, groups in API order as anchored `<h3>` regions, the `renderGroup`, `renderGroups` and
 *   `renderEmptyGroup` hooks, the empty state's one-sentence reason (hidden and suppressed
 *   cards, else the data-gap sentence of the section's `incomplete_dataset` findings, else the
 *   leading withheld line, else the first finding that explains the absence, else
 *   "No rule fired for this window", with no list repeated inside it and long identifiers
 *   wrapping at their underscores), the withheld list and the "Not shown" counts;
 * - the confirmations of items a feedback recorded on the page has hidden: placed in the hidden
 *   card's own position in API order (also in an emptied replaced-data or held block), never a
 *   batched entry, a live region or a Tab stop, keeping a group whose every item was hidden on
 *   the page in place of the empty state, every hidden count unchanged, and gone once a rerun
 *   drops the item or shows it again.
 *
 * The frame fetches nothing and never renders a card itself, so every test passes a plain
 * `renderItem` and renders without a network stub; only the confirmation tests wrap the frame
 * in the `ActionOutcomeProvider` that records them.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  Finding,
  FindingCode,
  Group,
  Item,
  ItemOverlay,
  Section,
  SectionKey,
} from '../../../api/types';
import { Button } from '../../../design-system';
import {
  makeEvidence,
  makeFinding,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeSection,
  makeWithheld,
} from '../../../test/fixtures';
import { LARGE_RENDER_TEST_TIMEOUT_MS } from '../../../test/timeouts';
import { ActionOutcomeProvider, useRecordFeedbackConfirmation } from '../ActionOutcomes';
import { HISTORICAL_LABEL } from '../RecommendationCard';
import { withheldLine } from '../WithheldList';
import { CARD_BATCH_SIZE, GROUP_BATCH_SIZE } from '../batches';
import {
  CardList,
  ConfirmedItems,
  DISCREPANCY_GROUP_ANCHOR,
  FieldList,
  HeldDisclosure,
  HistoricalItems,
  NO_RULE_FIRED_REASON,
  REPLACED_DATA_LABEL,
  SUPPRESSION_LABELS,
  SectionFrame,
  findingTone,
  groupAnchorId,
  groupRepeatsSectionTitle,
  incompleteDataReason,
  isConfirmed,
  isHeld,
  isHidden,
  isHistorical,
  itemAnchorId,
  itemIdentity,
  itemKeys,
  notShownReason,
  partitionGroup,
  placeConfirmations,
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

/**
 * An item computed from data that has since been replaced: the overlay marks it historical and
 * sets `held_now` with the replaced-data reason (AAP 0.3.2). With `heldByRun`, the run had also
 * held it for a discrepancy.
 */
function historicalItem(id: number, action: string, heldByRun = false): Item {
  return makeItem({
    id,
    rank: id,
    action,
    held: heldByRun,
    current: makeItemOverlay({
      historical: true,
      held_now: true,
      held_reason_now: 'Computed from data that has since been replaced',
      data_status_now: 'live',
    }),
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

/**
 * An `incomplete_dataset` finding as the run's P4 step records it: the message names the dataset
 * and its causes, and the evidence carries the dataset, its completeness row and `reasons`.
 */
function incompleteFinding(
  dataset: string,
  reasons: readonly string[],
  section: SectionKey = 'keywords',
): Finding {
  return makeFinding({
    code: 'incomplete_dataset',
    section,
    entity: null,
    message: `${dataset} is not complete over the window: ${reasons.join('; ')}`,
    evidence: {
      dataset,
      dataset_id: 7,
      completeness: 'incomplete',
      covered_from: '2026-04-13',
      covered_through: '2026-10-09',
      reason: null,
      reasons: [...reasons],
      items: 0,
    },
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

/** The frame's empty state (the design-system `EmptyState` root), or `null` when it has none. */
function emptyState(): Element | null {
  return document.querySelector('.ds-emptystate');
}

/** The text of the empty state's title, the frame's one-sentence reason. */
function emptyTitle(): string | null | undefined {
  return emptyState()?.querySelector('.ds-emptystate__title')?.textContent;
}

/**
 * `text` as Testing Library's text queries match a rendered node: the no-break spaces (U+00A0)
 * that join every "observed / required" figure collapsed to plain spaces. `textContent` keeps
 * them, so exact assertions on it compare `text` itself.
 */
function asQueriedText(text: string): string {
  return text.replaceAll('\u00A0', ' ');
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
// Group titles
// ---------------------------------------------------------------------------------------------

describe('groupRepeatsSectionTitle', () => {
  it('matches a group titled as its section, trimmed and ignoring case', () => {
    const budget = makeSection('budget');
    const exact = makeGroup({ key: 'changes', title: 'Budget changes' });
    const padded = makeGroup({ key: 'changes', title: ' BUDGET changes ' });
    expect(groupRepeatsSectionTitle(budget, exact)).toBe(true);
    expect(groupRepeatsSectionTitle(budget, padded)).toBe(true);
  });

  it('does not match a group with a title of its own', () => {
    const pause = makeGroup({ key: 'pause', title: 'Pause' });
    expect(groupRepeatsSectionTitle(makeSection('campaigns'), pause)).toBe(false);
    expect(
      groupRepeatsSectionTitle(
        makeSection('budget'),
        makeGroup({ key: 'changes', title: 'Budget changes (shared)' }),
      ),
    ).toBe(false);
  });

  it('compares a blank title as the key its heading falls back to', () => {
    expect(
      groupRepeatsSectionTitle(makeSection('budget'), makeGroup({ key: 'changes', title: ' ' })),
    ).toBe(false);
    expect(
      groupRepeatsSectionTitle(
        makeSection('budget', { title: '' }),
        makeGroup({ key: 'budget', title: '' }),
      ),
    ).toBe(true);
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

  it('treats an item computed from replaced data as historical, never as held', () => {
    expect(isHistorical(visibleItem(1, 'Pause'))).toBe(false);
    expect(isHistorical(heldNowItem(2, 'Pause'))).toBe(false);
    // The overlay's held_now on a historical item is the replaced data, not a discrepancy hold
    // (AAP 0.3.2), so no discrepancy resolution can lift it.
    expect(isHistorical(historicalItem(3, 'Pause'))).toBe(true);
    expect(isHeld(historicalItem(3, 'Pause'))).toBe(false);
    expect(isHeld(historicalItem(4, 'Pause', true))).toBe(false);
  });

  it('labels the replaced-data block with the words of the historical card badge', () => {
    expect(REPLACED_DATA_LABEL).toBe(HISTORICAL_LABEL);
  });

  it('treats an item as hidden only while current.hidden_by is set', () => {
    expect(isHidden(visibleItem(1, 'Pause'))).toBe(false);
    expect(isHidden(hiddenItem(2, 'Pause'))).toBe(true);
    for (const kind of ['dismiss', 'snooze', 'done'] as const) {
      expect(isHidden(makeItem({ current: makeItemOverlay({ hidden_by: kind }) }))).toBe(true);
    }
  });

  it('partitions a group into visible, historical, held and hidden items, keeping the API order', () => {
    const hiddenHistorical = historicalItem(9, 'hidden and historical');
    hiddenHistorical.current.hidden_by = 'snooze';
    const group = makeGroup({
      key: 'pause',
      title: 'Pause',
      items: [
        heldItem(1, 'held first'),
        historicalItem(7, 'historical first'),
        visibleItem(2, 'visible first'),
        hiddenItem(3, 'hidden'),
        heldNowItem(4, 'held now'),
        historicalItem(8, 'historical and held by the run', true),
        hiddenItem(5, 'hidden and held', true),
        hiddenHistorical,
        visibleItem(6, 'visible second'),
      ],
    });

    const { visible, historical, held, hiddenCount } = partitionGroup(group);

    expect(visible.map((item) => item.id)).toEqual([2, 6]);
    expect(historical.map((item) => item.id)).toEqual([7, 8]);
    expect(held.map((item) => item.id)).toEqual([1, 4]);
    // A hidden item is counted whether or not it is also held or historical.
    expect(hiddenCount).toBe(3);
  });

  it('partitions an empty group into nothing', () => {
    expect(partitionGroup(makeGroup())).toEqual({
      visible: [],
      historical: [],
      held: [],
      hiddenCount: 0,
    });
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

    // The controlled region always exists, so aria-controls resolves, but its content is not
    // mounted until the first expansion.
    const region = document.getElementById('group-campaigns-pause-held');
    expect(region).not.toBeNull();
    expect(region?.hidden).toBe(true);
    expect(region?.childElementCount).toBe(0);
    expect(region?.textContent).toBe('');
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
    expect(region?.textContent).toBe('Held card');

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(region?.hidden).toBe(true);
  });

  it('mounts its content on the first expansion and keeps it mounted while collapsed', () => {
    const mounts = vi.fn<() => void>();
    /** Held content that reports each mount and keeps a local count. */
    function Probe() {
      const [clicks, setClicks] = useState(0);
      useEffect(() => {
        mounts();
      }, []);
      return (
        <Button onClick={() => setClicks((count) => count + 1)}>
          {`Held card ${String(clicks)}`}
        </Button>
      );
    }
    render(
      <HeldDisclosure id="lazy-held" count={1}>
        <Probe />
      </HeldDisclosure>,
    );
    const toggle = screen.getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    const region = document.getElementById('lazy-held');
    expect(mounts).not.toHaveBeenCalled();
    expect(region?.textContent).toBe('');

    fireEvent.click(toggle);
    expect(mounts).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Held card 0' }));

    // Collapsed again: hidden, yet still mounted with its state.
    fireEvent.click(toggle);
    expect(region?.hidden).toBe(true);
    expect(region?.textContent).toBe('Held card 1');
    fireEvent.click(toggle);
    expect(mounts).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Held card 1' })).toBeTruthy();
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

  describe('a hash naming a held card', () => {
    /** A stand-in for `Element.prototype.scrollIntoView`, which jsdom does not implement. */
    let scrollIntoView: ReturnType<typeof vi.fn>;

    beforeEach(() => {
      scrollIntoView = vi.fn();
      Object.defineProperty(Element.prototype, 'scrollIntoView', {
        configurable: true,
        writable: true,
        value: scrollIntoView,
      });
    });

    afterEach(() => {
      window.history.replaceState(null, '', window.location.pathname);
      Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
    });

    /** Sets the hash as following an in-page link does, and dispatches its `hashchange`. */
    function followHash(hash: string): void {
      act(() => {
        window.history.replaceState(null, '', hash);
        window.dispatchEvent(new HashChangeEvent('hashchange'));
      });
    }

    /** A disclosure holding the cards `item-501` and `item-502`, as a group's held cards. */
    function renderHeldCards(anchors?: readonly string[]) {
      return render(
        <HeldDisclosure id="group-budget-changes-held" count={2} anchors={anchors}>
          <article id="item-501" aria-label="Held B1">
            Held B1
          </article>
          <article id="item-502" aria-label="Held B3">
            Held B3
          </article>
        </HeldDisclosure>,
      );
    }

    /** The disclosure's toggle. */
    function toggle(): HTMLElement {
      return screen.getByRole('button', { name: 'Held until discrepancies are resolved (2)' });
    }

    it('starts expanded when the page loads at one, and scrolls the card into view without focusing it', () => {
      window.history.replaceState(null, '', '#item-502');
      const focused = document.activeElement;
      renderHeldCards(['item-501', 'item-502']);

      expect(toggle().getAttribute('aria-expanded')).toBe('true');
      expect(document.getElementById('group-budget-changes-held')?.hidden).toBe(false);
      const target = screen.getByRole('article', { name: 'Held B3' });
      expect(scrollIntoView.mock.contexts).toEqual([target]);
      expect(scrollIntoView.mock.calls[0]?.[0]).toEqual({ block: 'start' });
      // Scrolling only: a hash present on mount, as on a page load, moves no focus, as with a
      // batched list's hash reveal.
      expect(document.activeElement).toBe(focused);
      expect(target.hasAttribute('tabindex')).toBe(false);
    });

    it('expands on a hash change naming one, scrolls the card into view, then focuses it', () => {
      renderHeldCards(['item-501', 'item-502']);
      expect(toggle().getAttribute('aria-expanded')).toBe('false');
      expect(screen.queryByRole('article')).toBeNull();
      const focus = vi.spyOn(HTMLElement.prototype, 'focus');

      // Never opened: the cards mount on this expansion.
      followHash('#item-501');
      expect(toggle().getAttribute('aria-expanded')).toBe('true');
      const target = screen.getByRole('article', { name: 'Held B1' });
      expect(scrollIntoView.mock.contexts).toEqual([target]);
      // Focus follows the link onto the card, after the scroll and without scrolling again; the
      // card is focusable only while it holds focus.
      expect(document.activeElement).toBe(target);
      expect(target.getAttribute('tabindex')).toBe('-1');
      expect(focus.mock.contexts).toEqual([target]);
      expect(focus.mock.calls).toEqual([[{ preventScroll: true }]]);
      expect(scrollIntoView.mock.invocationCallOrder[0]).toBeLessThan(
        focus.mock.invocationCallOrder[0] ?? 0,
      );
      focus.mockRestore();
      target.blur();
      expect(target.hasAttribute('tabindex')).toBe(false);
    });

    it('can collapse again, and a later hash change shows, scrolls to and focuses the card still mounted', () => {
      renderHeldCards(['item-501', 'item-502']);
      // Opened with the button, then collapsed again: the cards stay mounted, hidden.
      fireEvent.click(toggle());
      const b3 = screen.getByRole('article', { name: 'Held B3' });
      fireEvent.click(toggle());
      expect(document.getElementById('group-budget-changes-held')?.hidden).toBe(true);
      act(() => {
        toggle().focus();
      });
      expect(document.activeElement).toBe(toggle());

      followHash('#item-502');
      expect(toggle().getAttribute('aria-expanded')).toBe('true');
      // The same card, kept mounted while collapsed, is shown again, scrolled to and focused.
      expect(screen.getByRole('article', { name: 'Held B3' })).toBe(b3);
      expect(scrollIntoView.mock.contexts).toEqual([b3]);
      expect(document.activeElement).toBe(b3);

      // The hold is not released: the cards stay behind the same control, which collapses them.
      fireEvent.click(toggle());
      expect(toggle().getAttribute('aria-expanded')).toBe('false');
      expect(document.getElementById('group-budget-changes-held')?.hidden).toBe(true);

      // A later link to the other card, collapsed again, expands, scrolls and focuses once more.
      followHash('#item-501');
      expect(toggle().getAttribute('aria-expanded')).toBe('true');
      const b1 = screen.getByRole('article', { name: 'Held B1' });
      expect(scrollIntoView.mock.contexts).toEqual([b3, b1]);
      expect(document.activeElement).toBe(b1);
      expect(b3.hasAttribute('tabindex')).toBe(false);
    });

    it('stays collapsed for a hash naming no held card, on mount and on a hash change', () => {
      window.history.replaceState(null, '', '#item-999');
      renderHeldCards(['item-501', 'item-502']);
      expect(toggle().getAttribute('aria-expanded')).toBe('false');

      followHash('#group-campaigns-discrepancies');
      followHash('#');
      expect(toggle().getAttribute('aria-expanded')).toBe('false');
      expect(document.getElementById('group-budget-changes-held')?.childElementCount).toBe(0);
      expect(scrollIntoView).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(document.body);
    });

    it('ignores the hash without anchors', () => {
      window.history.replaceState(null, '', '#item-501');
      renderHeldCards();
      expect(toggle().getAttribute('aria-expanded')).toBe('false');

      followHash('#item-502');
      expect(toggle().getAttribute('aria-expanded')).toBe('false');
      expect(scrollIntoView).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(document.body);
    });

    it("expands a frame group's held cards for a hash naming one, beyond their first batch", () => {
      const held = Array.from({ length: CARD_BATCH_SIZE + 5 }, (_, index) =>
        heldItem(700 + index, `Held action ${String(700 + index)}`),
      );
      const others = makeGroup({ key: 'pause', title: 'Pause', items: [heldItem(800, 'Other')] });
      window.history.replaceState(null, '', `#${itemAnchorId(722)}`);
      renderFrame({
        section: makeSection('budget', {
          groups: [makeGroup({ key: 'changes', title: 'Changes', items: held }), others],
        }),
        renderItem: (item) => (
          <article id={itemAnchorId(item.id)} aria-label={item.action}>
            {item.action}
          </article>
        ),
      });

      const changes = screen.getByRole('region', { name: 'Changes' });
      const heldToggle = within(changes).getByRole('button', {
        name: `Held until discrepancies are resolved (${String(CARD_BATCH_SIZE + 5)})`,
      });
      expect(heldToggle.getAttribute('aria-expanded')).toBe('true');
      // The held card list reveals its batches through the named card, which is scrolled to.
      const target = within(changes).getByRole('article', { name: 'Held action 722' });
      expect(scrollIntoView.mock.contexts).toContain(target);
      // A page load at the card moves no focus.
      expect(document.activeElement).toBe(document.body);
      // Another group's held cards, which the hash does not name, stay collapsed and unmounted.
      const pause = screen.getByRole('region', { name: 'Pause' });
      expect(
        within(pause)
          .getByRole('button', { name: 'Held until discrepancies are resolved (1)' })
          .getAttribute('aria-expanded'),
      ).toBe('false');
      expect(within(pause).queryByRole('article')).toBeNull();
    });

    it.each([
      ['never opened', false],
      ['opened and collapsed again', true],
    ])(
      "focuses a frame group's held card a hash change names beyond their first batch once, %s",
      (_label, openedBefore) => {
        const held = Array.from({ length: CARD_BATCH_SIZE + 5 }, (_, index) =>
          heldItem(700 + index, `Held action ${String(700 + index)}`),
        );
        renderFrame({
          section: makeSection('budget', {
            groups: [makeGroup({ key: 'changes', title: 'Changes', items: held })],
          }),
          renderItem: (item) => (
            <article id={itemAnchorId(item.id)} aria-label={item.action}>
              {item.action}
            </article>
          ),
        });
        const changes = screen.getByRole('region', { name: 'Changes' });
        const heldToggle = within(changes).getByRole('button', {
          name: `Held until discrepancies are resolved (${String(CARD_BATCH_SIZE + 5)})`,
        });
        if (openedBefore) {
          fireEvent.click(heldToggle);
          // Only the first batch is mounted, without the card the hash will name.
          expect(document.getElementById(itemAnchorId(722))).toBeNull();
          fireEvent.click(heldToggle);
        }
        const focus = vi.spyOn(HTMLElement.prototype, 'focus');

        followHash(`#${itemAnchorId(722)}`);

        expect(heldToggle.getAttribute('aria-expanded')).toBe('true');
        const target = within(changes).getByRole('article', { name: 'Held action 722' });
        expect(scrollIntoView.mock.contexts).toContain(target);
        // The card list, which reveals the batch holding the card, and the disclosure both act
        // on the hash, yet the card takes focus exactly once.
        expect(document.activeElement).toBe(target);
        expect(focus.mock.contexts).toEqual([target]);
        focus.mockRestore();
      },
    );
  });
});

// ---------------------------------------------------------------------------------------------
// HistoricalItems
// ---------------------------------------------------------------------------------------------

describe('HistoricalItems', () => {
  it('shows its items openly in a group named by the replaced-data label and count', () => {
    render(
      <HistoricalItems id="group-creative-campaign-1-historical" count={2}>
        <span>Historical card A</span>
        <span>Historical card B</span>
      </HistoricalItems>,
    );

    const block = screen.getByRole('group', { name: 'Computed from replaced data (2)' });
    expect(block.id).toBe('group-creative-campaign-1-historical');
    expect(block.hidden).toBe(false);
    // The visible label repeats the group's name and is hidden from assistive technology, so
    // the name is announced once; nothing in the block is a toggle.
    const label = within(block).getByText('Computed from replaced data (2)');
    expect(label.getAttribute('aria-hidden')).toBe('true');
    expect(within(block).queryByRole('button')).toBeNull();
    expect(block.textContent).toBe(
      'Computed from replaced data (2)Historical card AHistorical card B',
    );
  });

  it('formats a large count with grouping', () => {
    render(
      <HistoricalItems id="many-historical" count={1234}>
        <span>Historical card</span>
      </HistoricalItems>,
    );
    expect(screen.getByRole('group', { name: 'Computed from replaced data (1,234)' })).toBeTruthy();
  });

  it.each([
    ['zero', 0],
    ['a negative count', -1],
    ['NaN', Number.NaN],
  ])('renders nothing for %s', (_label, count) => {
    const { container } = render(
      <HistoricalItems id="nothing-historical" count={count}>
        <span>Historical card</span>
      </HistoricalItems>,
    );
    expect(container.childElementCount).toBe(0);
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
          { key: 'spend', label: 'Spend', value: 'USD 412.30' },
          { key: 'conversions', label: 'Conversions', value: 0 },
        ]}
      />,
    );
    expect(listLines('Keyword details')).toEqual([
      'Term: crm software',
      'Spend: USD 412.30',
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
          { key: 'spend', label: 'Spend', value: 'USD 1.00' },
          { key: 'spend', label: 'Spend', value: 'USD 2.00' },
        ]}
      />,
    );
    const list = screen.getByRole('list');
    expect(list.getAttribute('aria-label')).toBeNull();
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent),
    ).toEqual(['Spend: USD 1.00', 'Spend: USD 2.00']);
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

describe('notShownReason', () => {
  it('names the cards hidden by feedback, then each suppression count, in one sentence', () => {
    expect(notShownReason(1)).toBe(
      'No actions to show: 1 hidden by feedback since this run (dismissed, snoozed or done)',
    );
    expect(
      notShownReason(1_200, [
        ['feedback_dismiss', 2],
        ['g1_blocked', 1],
        ['p9_future_reason', 3],
      ]),
    ).toBe(
      'No actions to show: 1,200 hidden by feedback since this run (dismissed, snoozed or done); ' +
        '2 dismissed; 1 spend increase blocked by the high-utilization guard; 3 p9_future_reason',
    );
    expect(notShownReason(0, [['g1_blocked', 3]])).toBe(
      'No actions to show: 3 spend increases blocked by the high-utilization guard',
    );
  });

  it('is null when nothing is hidden or suppressed, and ignores a count that is not positive', () => {
    expect(notShownReason(0)).toBeNull();
    expect(notShownReason(-1, [])).toBeNull();
    expect(notShownReason(Number.NaN)).toBeNull();
  });
});

describe('incompleteDataReason', () => {
  const NO_GOOGLE = 'no active Google dataset';
  const FROM_13 = 'covers from 2026-04-13, after the window start 2026-01-01';
  const FROM_14 = 'covers from 2026-04-14, after the window start 2026-01-01';

  it('summarises the datasets that share a cause in one sentence, in API order', () => {
    expect(
      incompleteDataReason([
        incompleteFinding('google.entities', [NO_GOOGLE]),
        incompleteFinding('google.metrics_daily', [NO_GOOGLE]),
        incompleteFinding('google.search_terms', [NO_GOOGLE]),
      ]),
    ).toBe(
      'Data does not fully cover this window: google.entities, google.metrics_daily, ' +
        'google.search_terms (no active Google dataset)',
    );
  });

  it('groups datasets by cause in first-seen order and names each dataset once', () => {
    expect(
      incompleteDataReason([
        incompleteFinding('hubspot.associations', [FROM_13], 'white_papers'),
        incompleteFinding('linkedin.analytics_daily', [FROM_14], 'white_papers'),
        incompleteFinding('hubspot.contacts', [FROM_13], 'white_papers'),
        incompleteFinding('hubspot.associations', [FROM_13], 'white_papers'),
        // Another code is not a data gap, whatever its evidence names.
        makeFinding({
          code: 'skipped_stage',
          section: 'white_papers',
          evidence: { dataset: 'hubspot.deal_search', reasons: [FROM_13] },
        }),
        incompleteFinding('linkedin.leads', [FROM_13], 'white_papers'),
      ]),
    ).toBe(
      'Data does not fully cover this window: hubspot.associations, hubspot.contacts, ' +
        `linkedin.leads (${FROM_13}); linkedin.analytics_daily (${FROM_14})`,
    );
  });

  it('joins a dataset\'s causes with "; ", trimmed, and ignores entries that are not text', () => {
    expect(
      incompleteDataReason([
        makeFinding({
          code: 'incomplete_dataset',
          evidence: {
            dataset: '  google.metrics_daily ',
            reasons: [' partition deferred ', 3, null, '', 'call budget exhausted'],
          },
        }),
        incompleteFinding('google.search_terms', ['partition deferred', 'call budget exhausted']),
      ]),
    ).toBe(
      'Data does not fully cover this window: google.metrics_daily, google.search_terms ' +
        '(partition deferred; call budget exhausted)',
    );
  });

  it('names a dataset without a cause when its finding records none', () => {
    expect(
      incompleteDataReason([
        incompleteFinding('linkedin.leads', []),
        makeFinding({ code: 'incomplete_dataset', evidence: { dataset: 'linkedin.segments' } }),
        makeFinding({
          code: 'incomplete_dataset',
          evidence: { dataset: 'linkedin.analytics_daily', reasons: [7, ' ', null] },
        }),
        incompleteFinding('hubspot.contacts', [FROM_13]),
        // A `reasons` that is not an array records no cause.
        makeFinding({
          code: 'incomplete_dataset',
          evidence: { dataset: 'hubspot.deal_search', reasons: FROM_13 },
        }),
      ]),
    ).toBe(
      'Data does not fully cover this window: linkedin.leads, linkedin.segments, ' +
        `linkedin.analytics_daily, hubspot.deal_search; hubspot.contacts (${FROM_13})`,
    );
  });

  it('skips a finding whose evidence names no dataset, and is null when none does', () => {
    const malformed: Finding[] = [
      makeFinding({ code: 'incomplete_dataset', evidence: { dataset: 42, reasons: [NO_GOOGLE] } }),
      makeFinding({ code: 'incomplete_dataset', evidence: { dataset: '  ', reasons: [NO_GOOGLE] } }),
      makeFinding({ code: 'incomplete_dataset', evidence: { reasons: [NO_GOOGLE] } }),
      // Payloads that bypassed the contract, as an older or newer API could send them.
      { ...makeFinding({ code: 'incomplete_dataset' }), evidence: null } as unknown as Finding,
      {
        ...makeFinding({ code: 'incomplete_dataset' }),
        evidence: ['google.entities'],
      } as unknown as Finding,
      makeFinding({
        code: 'pipeline_unavailable',
        evidence: { dataset: 'hubspot.contacts', reasons: [FROM_13] },
      }),
    ];
    expect(incompleteDataReason(malformed)).toBeNull();
    expect(incompleteDataReason([])).toBeNull();
    expect(
      incompleteDataReason([...malformed, incompleteFinding('google.entities', [NO_GOOGLE])]),
    ).toBe('Data does not fully cover this window: google.entities (no active Google dataset)');
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

  it('renders a group whose title repeats the section title with no heading, keeping its anchor', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [
          makeGroup({
            key: 'changes',
            title: ' budget CHANGES ',
            items: [visibleItem(1, 'B1'), heldItem(2, 'B3')],
          }),
          makeGroup({ key: 'shared', title: 'Shared budgets', items: [visibleItem(3, 'B2')] }),
        ],
      }),
    });

    // The section title is shown once, as the <h2>; only the differing group has an <h3>.
    const section = screen.getByRole('region', { name: 'Budget changes' });
    expect(section.id).toBe('section-budget');
    expect(screen.getAllByRole('heading', { name: /^budget changes$/iu })).toEqual([
      within(section).getByRole('heading', { level: 2 }),
    ]);
    expect(
      within(section)
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(['Shared budgets']);
    expect(within(section).getAllByRole('region').map((region) => region.id)).toEqual([
      'group-budget-shared',
    ]);

    // The heading-less group keeps its anchor on a plain container holding its cards in order,
    // then its held disclosure, ahead of the next group.
    const group = document.getElementById('group-budget-changes');
    if (group === null) {
      throw new Error('the group anchor is missing');
    }
    expect(group.tagName).toBe('DIV');
    expect(group.getAttribute('role')).toBeNull();
    expect(group.getAttribute('aria-label')).toBeNull();
    expect(within(group).getByTestId('item-1').textContent).toBe('changes: B1');
    expect(
      within(group).getByRole('button', { name: 'Held until discrepancies are resolved (1)' }),
    ).toBeTruthy();
    expect(document.getElementById('group-budget-changes-held')).not.toBeNull();
    expect(within(group).queryByTestId('item-3')).toBeNull();
    expect(follows(group, screen.getByRole('region', { name: 'Shared budgets' }))).toBe(true);
  });

  it('keeps the <h3> region of a group whose title differs from the section title', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [makeGroup({ key: 'changes', title: 'Changes', items: [visibleItem(1, 'B1')] })],
      }),
    });

    const group = screen.getByRole('region', { name: 'Changes' });
    expect(group.tagName).toBe('SECTION');
    expect(group.id).toBe('group-budget-changes');
    const heading = within(group).getByRole('heading', { level: 3, name: 'Changes' });
    expect(group.getAttribute('aria-labelledby')).toBe(heading.id);
    expect(within(group).getByTestId('item-1').textContent).toBe('changes: B1');
  });

  it('keeps the anchor of an empty group renderEmptyGroup fills when its title repeats', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [makeGroup({ key: 'changes', title: 'Budget changes', items: [] })],
      }),
      renderEmptyGroup: () => <div data-testid="empty-group">Nothing to change</div>,
    });

    const group = document.getElementById('group-budget-changes');
    expect(group?.tagName).toBe('DIV');
    expect(group?.contains(screen.getByTestId('empty-group'))).toBe(true);
    expect(screen.getAllByRole('heading', { name: 'Budget changes' })).toHaveLength(1);
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
    const lines = () =>
      within(group)
        .getAllByTestId(/^item-/u)
        .map((line) => line.textContent);
    const renderedIds = () =>
      renderItem.mock.calls.map(([item]) => item.id).sort((left, right) => left - right);
    // Collapsed held cards are not even built until the disclosure is first opened.
    expect(lines()).toEqual(['pause: Pause first', 'pause: Pause second']);
    expect(renderedIds()).toEqual([12, 15]);
    // The hidden card is counted, never rendered.
    expect(screen.queryByText('pause: Dismissed since the run')).toBeNull();

    const toggle = within(group).getByRole('button', {
      name: 'Held until discrepancies are resolved (2)',
    });
    const heldRegion = document.getElementById('group-campaigns-pause-held');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe('group-campaigns-pause-held');
    expect(heldRegion?.hidden).toBe(true);
    expect(heldRegion?.childElementCount).toBe(0);

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(heldRegion?.hidden).toBe(false);
    expect(lines()).toEqual([
      'pause: Pause first',
      'pause: Pause second',
      'pause: Held by the run',
      'pause: Held since the run',
    ]);
    expect(heldRegion?.contains(screen.getByTestId('item-11'))).toBe(true);
    expect(heldRegion?.contains(screen.getByTestId('item-14'))).toBe(true);
    expect(heldRegion?.contains(screen.getByTestId('item-12'))).toBe(false);
    expect(new Set(renderedIds())).toEqual(new Set([11, 12, 14, 15]));

    expect(listLines('Not shown')).toEqual(['1 hidden since this run']);
  });

  it('shows cards computed from replaced data openly, after the visible ones and before the held disclosure', () => {
    renderFrame({
      section: makeSection('creative', {
        groups: [
          makeGroup({
            key: 'campaign:1',
            title: 'Lead Gen',
            items: [
              historicalItem(21, 'Replaced pair'),
              visibleItem(22, 'Next test'),
              heldItem(23, 'Held pair'),
              historicalItem(24, 'Replaced and held by the run', true),
            ],
          }),
        ],
      }),
    });

    const group = screen.getByRole('region', { name: 'Lead Gen' });
    const block = within(group).getByRole('group', { name: 'Computed from replaced data (2)' });
    expect(block.id).toBe('group-creative-campaign-1-historical');
    expect(block.hidden).toBe(false);
    // Both historical cards, in API order, readable without operating any control.
    expect(
      within(block)
        .getAllByTestId(/^item-/u)
        .map((line) => line.textContent),
    ).toEqual(['campaign:1: Replaced pair', 'campaign:1: Replaced and held by the run']);

    // Only the discrepancy hold sits behind the disclosure, which comes last.
    const toggle = within(group).getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    expect(follows(screen.getByTestId('item-22'), block)).toBe(true);
    expect(follows(block, toggle)).toBe(true);
    const heldRegion = document.getElementById('group-creative-campaign-1-held');
    expect(heldRegion?.contains(screen.getByTestId('item-21'))).toBe(false);
    expect(heldRegion?.contains(screen.getByTestId('item-24'))).toBe(false);
    expect(heldRegion?.contains(screen.getByTestId('item-22'))).toBe(false);
  });

  it('keeps a group whose only items are computed from replaced data, with no held disclosure and no empty state', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [
          makeGroup({ key: 'changes', title: 'Changes', items: [historicalItem(1, 'B4')] }),
        ],
      }),
    });
    const group = screen.getByRole('region', { name: 'Changes' });
    expect(group.id).toBe('group-budget-changes');
    expect(
      within(group).getByRole('group', { name: 'Computed from replaced data (1)' }).textContent,
    ).toContain('changes: B4');
    expect(
      screen.queryByRole('button', { name: /^Held until discrepancies are resolved/u }),
    ).toBeNull();
    expect(document.getElementById('group-budget-changes-held')).toBeNull();
    expect(emptyState()).toBeNull();
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
    expect(emptyState()).toBeNull();
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
    expect(emptyState()).toBeNull();
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
    expect(emptyState()).toBeNull();
  });

  it('lets renderGroup replace the body of each group with shown items, with its parts', () => {
    const seen: GroupParts[] = [];
    renderFrame({
      section: makeSection('white_papers', {
        groups: [
          makeGroup({
            key: 'cohort:EUR',
            title: 'EUR',
            items: [
              visibleItem(1, 'Double down'),
              heldItem(2, 'Held row'),
              hiddenItem(3, 'Gone'),
              historicalItem(4, 'Replaced row'),
            ],
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
    expect(parts?.historical.map((item) => item.id)).toEqual([4]);
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
    expect(
      within(region).getByRole('group', { name: 'Computed from replaced data (1)' }).textContent,
    ).toContain('cohort:EUR: Replaced row');
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
          makeGroup({ key: 'expand', title: 'Expand', items: [historicalItem(3, 'K2')] }),
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
    expect(parts.map((part) => part.historical.length)).toEqual([0, 1, 0]);
    expect(parts.map((part) => part.held.length)).toEqual([0, 0, 1]);

    expect(screen.getByTestId('tabs').textContent).toBe('Block (1)Expand (0)Review (0)');
    // The frame renders no group region of its own and ignores renderGroup.
    expect(screen.queryAllByRole('heading', { level: 3 })).toHaveLength(0);
    expect(renderGroup).not.toHaveBeenCalled();
    expect(emptyState()).toBeNull();
  });

  it('calls renderGroups, with no empty state, when the only shown item is computed from replaced data', () => {
    const renderGroups = vi.fn(() => <div data-testid="tabs">tabs</div>);
    renderFrame({
      section: makeSection('keywords', {
        groups: [makeGroup({ key: 'review', title: 'Review', items: [historicalItem(1, 'K3')] })],
      }),
      renderGroups,
    });

    expect(renderGroups).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('tabs')).toBeTruthy();
    expect(emptyState()).toBeNull();
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
    expect(emptyTitle()).toBe(
      'No actions to show: 1 hidden by feedback since this run (dismissed, snoozed or done)',
    );
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
        NO_RULE_FIRED_REASON,
      );
      expect(NO_RULE_FIRED_REASON).toBe('No rule fired for this window');
      // One sentence, as the title, and no reason body.
      expect(empty?.querySelector('.ds-emptystate__reason')).toBeNull();
      expect(screen.queryByRole('list', { name: 'Not shown' })).toBeNull();
      expect(screen.queryByText('Withheld for sample size')).toBeNull();
    });

    it('wraps long identifiers of a finding at their underscores, in its banner and the empty-state sentence', () => {
      const message =
        'No CRM link for Google campaigns: HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and ' +
        'HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured.';
      renderFrame({
        section: makeSection('campaigns', {
          findings: [makeFinding({ code: 'no_crm_link', section: 'campaigns', message })],
          groups: [makeGroup({ key: 'pause', title: 'Pause', items: [] })],
        }),
      });

      const region = screen.getByRole('region', { name: 'Campaigns to pause or launch' });
      const banner = region.querySelector('.ds-banner__title');
      // The only finding is the empty state's one sentence, its title, with no reason body.
      const sentence = region.querySelector('.ds-emptystate__title');
      expect(region.querySelector('.ds-emptystate__reason')).toBeNull();
      for (const node of [banner, sentence]) {
        // The text is the API's, unchanged; only <wbr> break opportunities are added.
        expect(node?.textContent).toBe(message);
        const breaks = node?.querySelectorAll('wbr') ?? [];
        expect(breaks).toHaveLength(7);
        for (const wbr of breaks) {
          expect(wbr.previousSibling?.textContent?.endsWith('_')).toBe(true);
        }
      }
    });

    it('states the leading withheld line\'s minimum as its reason, and the full withheld list follows it', () => {
      const withheld = makeWithheld();
      renderFrame({
        section: makeSection('keywords', {
          groups: [makeGroup({ items: [] })],
          withheld: [withheld],
        }),
      });

      const empty = emptyState();
      expect(empty).not.toBeNull();
      // AAP 0.5.5's example sentence, its minimum the recorded sample.required (37 clicks).
      expect(emptyTitle()).toBe('No search term met the K1 minimum of 37 clicks');
      expect(empty?.querySelector('.ds-emptystate__reason')).toBeNull();
      // The withheld line is the list's alone: the empty state does not repeat it.
      expect(empty?.textContent).not.toContain(withheldLine(withheld));
      expect(screen.getAllByText(asQueriedText(withheldLine(withheld)))).toHaveLength(1);

      // The WithheldList still renders in full, below the empty state.
      const list = screen.getByRole('list', { name: 'Withheld for sample size' });
      expect(listLines('Withheld for sample size')).toEqual([withheldLine(withheld)]);
      if (empty !== null) {
        expect(follows(empty, list)).toBe(true);
      }
      expect(screen.queryByRole('list', { name: 'Not shown' })).toBeNull();
    });

    it('states an account-wide withheld line\'s reason before any finding', () => {
      renderFrame({
        section: makeSection('keywords', {
          groups: [makeGroup({ items: [] })],
          findings: [
            makeFinding({
              code: 'k1_no_baseline',
              message: 'Block candidates withheld account-wide: too few baseline conversions.',
            }),
          ],
          withheld: [
            makeWithheld({
              entity: null,
              reason: 'baseline 9 conversions of 10 required',
              sample: { observed: 9, required: 10, unit: 'conversions' },
            }),
          ],
        }),
      });

      expect(emptyTitle()).toBe('K1 withheld: baseline 9 conversions of 10 required');
      // The finding stays a banner above, once.
      expect(
        screen.getAllByText('Block candidates withheld account-wide: too few baseline conversions.'),
      ).toHaveLength(1);
    });

    it('states the data-gap sentence of its incomplete datasets, never one finding\'s banner', () => {
      const findings = [
        incompleteFinding('google.entities', ['no active Google dataset']),
        incompleteFinding('google.metrics_daily', ['no active Google dataset']),
        incompleteFinding('google.search_terms', ['no active Google dataset']),
      ];
      renderFrame({
        section: makeSection('keywords', {
          groups: [
            makeGroup({ key: 'block', title: 'Block', items: [] }),
            makeGroup({ key: 'review', title: 'Review', items: [] }),
          ],
          findings,
        }),
      });

      expect(emptyTitle()).toBe(
        'Data does not fully cover this window: google.entities, google.metrics_daily, ' +
          'google.search_terms (no active Google dataset)',
      );
      expect(emptyState()?.querySelector('.ds-emptystate__reason')).toBeNull();
      // Each finding stays one banner above, and the empty state's title is none of them.
      const region = screen.getByRole('region', { name: 'Keywords to adjust' });
      const banners = [...region.querySelectorAll('.ds-banner__title')].map(
        (node) => node.textContent,
      );
      expect(banners).toEqual(findings.map((finding) => finding.message));
      expect(banners).not.toContain(emptyTitle());
      for (const finding of findings) {
        expect(emptyState()?.textContent).not.toContain(finding.message);
        expect(screen.getAllByText(finding.message)).toHaveLength(1);
      }
    });

    it('puts the data-gap sentence before a finding that comes first in API order', () => {
      const from13 = 'covers from 2026-04-13, after the window start 2026-01-01';
      renderFrame({
        section: makeSection('budget', {
          groups: [makeGroup({ key: 'changes', title: 'Budget changes', items: [] })],
          findings: [
            makeFinding({ code: 'pipeline_unavailable', message: 'Pipeline unavailable.' }),
            incompleteFinding('google.metrics_daily', [from13], 'budget'),
            incompleteFinding('linkedin.analytics_daily', [from13], 'budget'),
          ],
        }),
      });

      expect(emptyTitle()).toBe(
        'Data does not fully cover this window: google.metrics_daily, ' +
          `linkedin.analytics_daily (${from13})`,
      );
      expect(screen.getAllByText('Pipeline unavailable.')).toHaveLength(1);
    });

    it('puts the data-gap sentence ahead of the leading withheld line, which stays listed below', () => {
      const withheld = makeWithheld();
      renderFrame({
        section: makeSection('keywords', {
          groups: [makeGroup({ items: [] })],
          findings: [incompleteFinding('google.search_terms', ['partition deferred'])],
          withheld: [withheld],
        }),
      });

      expect(emptyTitle()).toBe(
        'Data does not fully cover this window: google.search_terms (partition deferred)',
      );
      // The withheld line is still listed in full below the empty state.
      expect(listLines('Withheld for sample size')).toEqual([withheldLine(withheld)]);
    });

    it('names the coverage gap, not one rule, for a window outside data coverage (run 10)', () => {
      const from15 = 'covers from 2026-04-15, after the window start 2026-01-01';
      const from14 = 'covers from 2026-04-14, after the window start 2026-01-01';
      const segments =
        'Segment data exists only for the preset windows 14, 28, 56, 90 ending 2026-10-10';
      renderFrame({
        section: makeSection('campaigns', {
          groups: [
            makeGroup({ key: 'discrepancies', title: 'Discrepancies', items: [] }),
            makeGroup({ key: 'launch', title: 'Launch ideas', items: [] }),
          ],
          findings: [
            makeFinding({
              code: 'segment_window_unavailable',
              section: 'campaigns',
              message: segments,
            }),
            incompleteFinding('google.metrics_daily', [from15], 'campaigns'),
            incompleteFinding('hubspot.contacts', [from14], 'campaigns'),
            incompleteFinding('linkedin.analytics_daily', [from15], 'campaigns'),
          ],
          withheld: [
            makeWithheld({
              rule_id: 'C3a',
              entity: null,
              reason: segments,
              sample: { observed: null, required: null, unit: 'segment windows' },
            }),
          ],
        }),
      });

      expect(emptyTitle()).toBe(
        `Data does not fully cover this window: google.metrics_daily, linkedin.analytics_daily ` +
          `(${from15}); hubspot.contacts (${from14})`,
      );
      expect(emptyTitle()).not.toContain('C3a withheld');
      // The C3a line records no sample shortfall, so its list reads "Withheld".
      expect(listLines('Withheld')).toEqual([`C3a · Account-wide: ${segments}`]);
    });

    it('keeps the withheld reason ahead of the findings when no finding names an incomplete dataset', () => {
      renderFrame({
        section: makeSection('keywords', {
          groups: [makeGroup({ items: [] })],
          findings: [
            makeFinding({ code: 'k1_no_baseline', message: 'K1 has no usable baseline.' }),
            // An incomplete_dataset finding whose evidence names no dataset gives no data-gap
            // sentence.
            makeFinding({
              code: 'incomplete_dataset',
              message: 'google.search_terms is not complete over the window',
              evidence: { dataset: null, reasons: ['partition deferred'] },
            }),
          ],
          withheld: [makeWithheld()],
        }),
      });

      // AAP 0.5.5's example sentence still leads without a coverage gap.
      expect(emptyTitle()).toBe('No search term met the K1 minimum of 37 clicks');
    });

    it('keeps the cards hidden by feedback and the suppression counts ahead of the data-gap sentence', () => {
      renderFrame({
        section: makeSection('budget', {
          groups: [makeGroup({ key: 'changes', title: 'Budget changes', items: [] })],
          findings: [incompleteFinding('google.metrics_daily', ['partition deferred'], 'budget')],
          withheld: [makeWithheld({ rule_id: 'B2' })],
          suppressed: { feedback_dismiss: 2 },
        }),
      });

      expect(emptyTitle()).toBe('No actions to show: 2 dismissed');
    });

    it('falls back to the first finding when no incomplete-dataset evidence names a dataset', () => {
      const unnamed =
        'google.search_terms is not complete over the window: no active Google dataset';
      renderFrame({
        section: makeSection('keywords', {
          groups: [makeGroup({ items: [] })],
          findings: [
            makeFinding({
              code: 'incomplete_dataset',
              message: unnamed,
              evidence: { dataset: 7, reasons: 'no active Google dataset' },
            }),
            makeFinding({ code: 'k1_no_baseline', message: 'K1 has no usable baseline.' }),
          ],
        }),
      });

      expect(emptyTitle()).toBe(unnamed);
    });

    it('states the first finding that explains the absence when nothing is withheld', () => {
      const incomplete =
        'google.metrics_daily is not complete over the window: covers from 2026-04-13, after ' +
        'the window start 2026-01-01';
      // Evidence that names no dataset, so no data-gap sentence applies and the first finding
      // that explains the absence is the reason.
      const unnamed = { completeness: 'incomplete' };
      renderFrame({
        section: makeSection('budget', {
          groups: [makeGroup({ key: 'changes', title: 'Budget changes', items: [] })],
          findings: [
            makeFinding({ code: 'no_crm_link', message: 'No CRM link for Google campaigns.' }),
            makeFinding({ code: 'incomplete_dataset', message: '   ', evidence: unnamed }),
            makeFinding({ code: 'incomplete_dataset', message: incomplete, evidence: unnamed }),
            makeFinding({ code: 'pipeline_unavailable', message: 'Pipeline unavailable.' }),
          ],
        }),
      });

      // An informational finding comes first in API order, but the incomplete dataset explains
      // the empty section; a blank message is skipped.
      expect(emptyTitle()).toBe(incomplete);
      expect(emptyState()?.querySelector('.ds-emptystate__reason')).toBeNull();
      // Every finding with a message is still a banner, and the others are not repeated below.
      const region = screen.getByRole('region', { name: 'Budget changes' });
      expect(emptyState()?.textContent).not.toContain('No CRM link for Google campaigns.');
      expect(emptyState()?.textContent).not.toContain('Pipeline unavailable.');
      expect(within(region).getByText('No CRM link for Google campaigns.')).toBeTruthy();
      expect(within(region).getByText('Pipeline unavailable.')).toBeTruthy();
    });

    it('falls back to an informational finding when it is the only one with a message', () => {
      renderFrame({
        section: makeSection('white_papers', {
          findings: [
            makeFinding({ code: 'pipeline_unavailable', message: '' }),
            makeFinding({
              code: 'skipped_stage',
              message: 'Qualified Lead: 2 contacts skipped this stage (not counted)',
            }),
          ],
        }),
      });

      expect(emptyTitle()).toBe('Qualified Lead: 2 contacts skipped this stage (not counted)');
    });

    it('names the suppression counts alone when nothing is hidden by feedback', () => {
      renderFrame({
        section: makeSection('budget', {
          groups: [makeGroup({ key: 'changes', title: 'Budget changes', items: [] })],
          withheld: [makeWithheld({ rule_id: 'B2' })],
          suppressed: { feedback_dismiss: 2, g1_blocked: 0 },
        }),
      });

      expect(emptyTitle()).toBe('No actions to show: 2 dismissed');
    });

    it('states why in one sentence: hidden cards and suppressions first, no list repeated', () => {
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

      expect(emptyTitle()).toBe(
        'No actions to show: 3 hidden by feedback since this run (dismissed, snoozed or done); ' +
          '2 snoozed; 1 spend increase blocked by the high-utilization guard; 4 unknown_reason',
      );
      const empty = emptyState();
      expect(empty?.querySelector('.ds-emptystate__reason')).toBeNull();
      // Neither the withheld lines nor the finding are repeated inside the empty state: the
      // finding is a banner above it, and the withheld lines are listed below it, once each.
      for (const w of withheld) {
        expect(empty?.textContent).not.toContain(withheldLine(w));
        expect(screen.getAllByText(asQueriedText(withheldLine(w)))).toHaveLength(1);
      }
      expect(empty?.textContent).not.toContain(finding.message);
      expect(screen.getAllByText(finding.message)).toHaveLength(1);
      // Groups without a shown item are left out.
      expect(screen.queryAllByRole('heading', { level: 3 })).toHaveLength(0);

      // The full withheld list follows under the default title, all four lines, each figure
      // joined by no-break spaces (U+00A0).
      expect(screen.getByText('Withheld for sample size')).toBeTruthy();
      expect(listLines('Withheld for sample size')).toEqual([
        'K1 · crm pricing comparison: clicks 30 of 37 required — 30\u00A0/\u00A037 clicks',
        'K3 · crm software: quality-score days 3 of 5 required — 3\u00A0/\u00A05 days',
        'K1 · Account-wide: fewer than 10 baseline conversions — 9\u00A0/\u00A010 conversions',
        'K2 · fourth term: conversions 2 of 3 required — 2\u00A0/\u00A03 conversions',
      ]);

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
      'B4 · Generic CRM — US: 3 weekend days of 4 required — 3\u00A0/\u00A04 weekend days',
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

// ---------------------------------------------------------------------------------------------
// Bounded batches
// ---------------------------------------------------------------------------------------------

describe('bounded batches', () => {
  /** A stand-in for `Element.prototype.scrollIntoView`, which jsdom does not implement. */
  let scrollIntoView: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    });
  });

  afterEach(() => {
    window.history.replaceState(null, '', window.location.pathname);
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  });

  /** A card as `RecommendationCard` anchors it: an `<article>` at `item-{id}`, showing its rank. */
  function renderCard(item: Item): ReactNode {
    return (
      <article id={itemAnchorId(item.id)} aria-label={item.action} data-rank={item.rank}>
        {`#${String(item.rank)} ${item.action}`}
      </article>
    );
  }

  /** `count` items built by `build`, with ids `offset + 1 …` and ranks `1 …` in API order. */
  function many(count: number, build: (id: number, action: string) => Item, offset = 0): Item[] {
    return Array.from({ length: count }, (_, index) => {
      const item = build(offset + index + 1, `Action ${String(offset + index + 1)}`);
      return { ...item, rank: index + 1 };
    });
  }

  /** The ranks of the cards rendered inside `scope`, in document order. */
  function ranksIn(scope: HTMLElement): number[] {
    return within(scope)
      .queryAllByRole('article')
      .map((article) => Number(article.dataset.rank));
  }

  /** The ranks `1..count`. */
  function ranksThrough(count: number): number[] {
    return Array.from({ length: count }, (_, index) => index + 1);
  }

  /** `count` groups `g1 …`, each holding `perGroup` visible items with ids `{g}001 …`. */
  function manyGroups(count: number, perGroup: number): Group[] {
    return Array.from({ length: count }, (_, index) =>
      makeGroup({
        key: `g${String(index + 1)}`,
        title: `Group ${String(index + 1)}`,
        items: many(perGroup, visibleItem, (index + 1) * 1000),
      }),
    );
  }

  it('anchors a card at item-{id}', () => {
    expect(itemAnchorId(1201)).toBe('item-1201');
  });

  it('mounts 250 visible cards one batch at a time, in API order, to the end', () => {
    renderFrame({
      section: makeSection('budget', {
        title: 'Budget changes',
        groups: [makeGroup({ key: 'items', title: 'Changes', items: many(250, visibleItem) })],
      }),
      renderItem: renderCard,
    });
    const group = screen.getByRole('region', { name: 'Changes' });

    expect(ranksIn(group)).toEqual(ranksThrough(CARD_BATCH_SIZE));
    expect(within(group).getByRole('status').textContent).toBe('Showing 20 of 250 actions');
    for (let shown = 40; shown <= 240; shown += 20) {
      fireEvent.click(within(group).getByRole('button', { name: 'Show 20 more actions' }));
      expect(ranksIn(group)).toEqual(ranksThrough(shown));
      // Focus continues at the first card the reveal showed.
      expect(document.activeElement).toBe(
        within(group).getByRole('article', { name: `Action ${String(shown - 19)}` }),
      );
    }
    fireEvent.click(within(group).getByRole('button', { name: 'Show 10 more actions' }));

    expect(ranksIn(group)).toEqual(ranksThrough(250));
    expect(within(group).queryByRole('button', { name: /^Show \d+ more/u })).toBeNull();
    expect(within(group).getByRole('status').textContent).toBe('Showing all 250 actions');
    expect(screen.queryByRole('alert')).toBeNull();
  }, LARGE_RENDER_TEST_TIMEOUT_MS);

  it('bounds the replaced-data block and the held cards on their own', () => {
    renderFrame({
      section: makeSection('budget', {
        groups: [
          makeGroup({
            key: 'items',
            title: 'Changes',
            items: [
              ...many(3, visibleItem),
              ...many(30, historicalItem, 100),
              ...many(45, heldItem, 200),
            ],
          }),
        ],
      }),
      renderItem: renderCard,
    });
    const group = screen.getByRole('region', { name: 'Changes' });

    // The visible cards are under one batch: no control of their own.
    const block = within(group).getByRole('group', { name: 'Computed from replaced data (30)' });
    expect(ranksIn(block)).toEqual(ranksThrough(20));
    expect(within(block).getByRole('status').textContent).toBe('Showing 20 of 30 actions');
    fireEvent.click(within(block).getByRole('button', { name: 'Show 10 more actions' }));
    expect(ranksIn(block)).toEqual(ranksThrough(30));
    expect(document.activeElement?.id).toBe('item-121');

    const heldRegion = document.getElementById('group-budget-items-held');
    if (heldRegion === null) {
      throw new Error('the held region is missing');
    }
    expect(ranksIn(heldRegion)).toEqual([]);
    fireEvent.click(
      within(group).getByRole('button', { name: 'Held until discrepancies are resolved (45)' }),
    );
    expect(ranksIn(heldRegion)).toEqual(ranksThrough(20));
    expect(within(heldRegion).getByRole('status').textContent).toBe('Showing 20 of 45 actions');
    fireEvent.click(within(heldRegion).getByRole('button', { name: 'Show 20 more actions' }));
    expect(ranksIn(heldRegion)).toEqual(ranksThrough(40));
    expect(document.activeElement?.id).toBe('item-221');
    // 3 visible + 30 historical + 40 held cards, never more than the batches revealed.
    expect(within(group).getAllByRole('article')).toHaveLength(73);
  });

  it('mounts the groups area one batch of groups at a time, focusing the first new group', () => {
    renderFrame({
      section: makeSection('creative', { groups: manyGroups(25, 1) }),
      renderItem: renderCard,
    });
    const section = screen.getByRole('region', { name: 'Copy/creative changes' });
    const groupRegions = (): string[] =>
      within(section)
        .getAllByRole('region')
        .map((region) => region.id);

    expect(groupRegions()).toHaveLength(GROUP_BATCH_SIZE);
    expect(groupRegions()[0]).toBe('group-creative-g1');
    const status = within(section).getAllByRole('status').at(-1);
    expect(status?.textContent).toBe('Showing 10 of 25 groups');

    fireEvent.click(within(section).getByRole('button', { name: 'Show 10 more groups' }));
    expect(groupRegions()).toHaveLength(20);
    expect(document.activeElement?.id).toBe('group-creative-g11');
    fireEvent.click(within(section).getByRole('button', { name: 'Show 5 more groups' }));
    expect(groupRegions()).toHaveLength(25);
    expect(groupRegions().at(-1)).toBe('group-creative-g25');
    expect(within(section).queryByRole('button', { name: /^Show \d+ more/u })).toBeNull();
  });

  it('reveals the group and the card a hash names on mount, beyond both first batches', () => {
    window.history.replaceState(null, '', `#${itemAnchorId(15025)}`);
    renderFrame({
      section: makeSection('creative', { groups: manyGroups(25, 50) }),
      renderItem: renderCard,
    });

    const group = screen.getByRole('region', { name: 'Group 15' });
    expect(group.id).toBe('group-creative-g15');
    // The group list shows two batches (through group 15), its card list two (through rank 25).
    expect(screen.queryByRole('region', { name: 'Group 20' })).not.toBeNull();
    expect(screen.queryByRole('region', { name: 'Group 21' })).toBeNull();
    expect(ranksIn(group)).toEqual(ranksThrough(40));
    const target = within(group).getByRole('article', { name: 'Action 15025' });
    expect(scrollIntoView.mock.contexts).toContain(target);
  });

  it('reveals the group a hash change names, and scrolls it into view', () => {
    renderFrame({
      section: makeSection('creative', { groups: manyGroups(25, 1) }),
      renderItem: renderCard,
    });
    expect(screen.queryByRole('region', { name: 'Group 23' })).toBeNull();

    act(() => {
      window.history.replaceState(null, '', '#group-creative-g23');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    const group = screen.getByRole('region', { name: 'Group 23' });
    expect(scrollIntoView.mock.contexts).toEqual([group]);
  });

  it('reveals and scrolls to a heading-less group a hash change names', () => {
    const repeating = makeGroup({
      key: 'campaign:99',
      title: 'Copy/creative changes',
      items: [visibleItem(99_001, 'Action 99001')],
    });
    renderFrame({
      section: makeSection('creative', { groups: [...manyGroups(GROUP_BATCH_SIZE, 1), repeating] }),
      renderItem: renderCard,
    });
    const anchor = 'group-creative-campaign-99';
    expect(document.getElementById(anchor)).toBeNull();

    // A hash naming the group's anchor reveals the batch holding it and scrolls the group to view.
    act(() => {
      window.history.replaceState(null, '', `#${anchor}`);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    const group = document.getElementById(anchor);
    expect(group?.tagName).toBe('DIV');
    expect(scrollIntoView.mock.contexts).toEqual([group]);
    expect(screen.queryByRole('button', { name: /^Show \d+ more/u })).toBeNull();
  });

  it('moves focus to a heading-less group that leads a revealed batch', () => {
    const repeating = makeGroup({
      key: 'campaign:99',
      title: 'Copy/creative changes',
      items: [visibleItem(99_001, 'Action 99001')],
    });
    renderFrame({
      section: makeSection('creative', { groups: [...manyGroups(GROUP_BATCH_SIZE, 1), repeating] }),
      renderItem: renderCard,
    });

    fireEvent.click(screen.getByRole('button', { name: 'Show 1 more group' }));
    const group = document.getElementById('group-creative-campaign-99');
    expect(group).not.toBeNull();
    expect(document.activeElement).toBe(group);
    // Focusable by script while it holds focus, never a Tab stop.
    expect(group?.getAttribute('tabindex')).toBe('-1');
    const card = within(group as HTMLElement).getByRole('article', { name: 'Action 99001' });
    expect(card.id).toBe(itemAnchorId(99_001));
  });

  it('hands renderGroups every group, unbatched, for the override to place', () => {
    const renderGroups = vi.fn((parts: readonly GroupParts[]) => <div>{parts.length}</div>);
    renderFrame({
      section: makeSection('keywords', { groups: manyGroups(25, 1) }),
      renderGroups,
    });

    expect(renderGroups.mock.calls[0]?.[0]).toHaveLength(25);
    expect(screen.queryByRole('button', { name: /^Show \d+ more/u })).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Card keys across runs
// ---------------------------------------------------------------------------------------------

describe('card keys across runs', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** A card with one action, anchored as `RecommendationCard` anchors it, at `item-{id}`. */
  function actionCard(item: Item): ReactNode {
    return (
      <article id={itemAnchorId(item.id)} aria-label={item.action}>
        <Button>{`Mark done: ${item.action}`}</Button>
      </article>
    );
  }

  /** A K1 item on the search term `term`, so items with different terms are different actions. */
  function termItem(id: number, term: string): Item {
    return makeItem({
      id,
      rank: id,
      action: `Add "${term}" as exact-match negative`,
      entity: { source: 'google', type: 'search_term', id: `61000201|7700119|${term}`, name: term },
    });
  }

  /** The same recommendation as a newer run recomputes it: every field kept but a new id. */
  function nextRun(item: Item): Item {
    return { ...item, id: item.id + 1000 };
  }

  /** A C0 item on campaign "Generic CRM — US" for the discrepancy `discrepancyId`. */
  function discrepancyItem(id: number, discrepancyId: number): Item {
    return makeItem({
      id,
      rank: id,
      rule_id: 'C0',
      action: `Resolve discrepancy ${String(discrepancyId)}`,
      entity: { source: 'google', type: 'campaign', id: '1234567890', name: 'Generic CRM — US' },
      evidence: makeEvidence({ extra: { discrepancy_id: discrepancyId, kind: 'status' } }),
    });
  }

  /** An item whose entity carries no id, with `entityId` as the payload sends it. */
  function unidentifiedItem(id: number, entityId: string | null): Item {
    return makeItem({
      id,
      rank: id,
      action: `Unidentified action ${String(id)}`,
      entity: { source: 'google', type: 'search_term', id: entityId, name: null },
    });
  }

  /**
   * Renders `view(items)` from state, starting with `initial`, and returns `show`, which hands it
   * new items as the refetched page of a newer run does.
   */
  function renderReplaceable(
    initial: readonly Item[],
    view: (items: readonly Item[]) => ReactNode,
  ): (next: readonly Item[]) => void {
    const control: { show?: (next: readonly Item[]) => void } = {};
    function Host(): ReactNode {
      const [items, setItems] = useState(initial);
      control.show = setItems;
      return view(items);
    }
    render(<Host />);
    return (next) => {
      act(() => {
        control.show?.(next);
      });
    };
  }

  /** A card list of `items`, as a section's group body renders it. */
  function cardList(items: readonly Item[]): ReactNode {
    return <CardList items={items} renderCard={actionCard} />;
  }

  it('gives the same recommendation in a newer run the same identity, and another one its own', () => {
    const item = termItem(61, 'crm demo');

    expect(itemIdentity(nextRun(item))).toBe(itemIdentity(item));
    expect(itemIdentity({ ...item, rule_id: 'K2' })).not.toBe(itemIdentity(item));
    expect(itemIdentity(termItem(61, 'crm trial'))).not.toBe(itemIdentity(item));
    expect(itemIdentity({ ...item, entity: { ...item.entity, source: 'linkedin' } })).not.toBe(
      itemIdentity(item),
    );
    expect(itemIdentity({ ...item, entity: { ...item.entity, type: 'keyword' } })).not.toBe(
      itemIdentity(item),
    );
  });

  it('never lets a separator inside an entity id make two identities equal', () => {
    const split = makeItem({
      entity: { source: 'google', type: 'keyword', id: '1|2', name: null },
    });
    const shifted = makeItem({
      entity: { source: 'google', type: 'keyword|1', id: '2', name: null },
    });
    const quoted = makeItem({
      entity: { source: 'google', type: 'keyword', id: '1","2', name: null },
    });

    expect(new Set([split, shifted, quoted].map(itemIdentity)).size).toBe(3);
  });

  it('tells the C0 discrepancies of one subject apart by their discrepancy id', () => {
    const first = discrepancyItem(1, 51);

    expect(itemIdentity(discrepancyItem(2, 52))).not.toBe(itemIdentity(first));
    expect(itemIdentity(nextRun(first))).toBe(itemIdentity(first));
  });

  it('falls back to a key for its run alone when the entity has no id', () => {
    expect(itemIdentity(unidentifiedItem(7, null))).toBe('run-item:7');
    expect(itemIdentity(unidentifiedItem(8, '  '))).toBe('run-item:8');
    // Only a cast can express the non-string id an untyped payload could still carry.
    expect(itemIdentity(unidentifiedItem(9, 42 as unknown as string))).toBe('run-item:9');
    expect(itemIdentity(nextRun(unidentifiedItem(7, null)))).not.toBe('run-item:7');
  });

  it('keys every item of a list uniquely, suffixing a repeated identity in list order', () => {
    const first = termItem(1, 'crm demo');
    const other = termItem(2, 'crm trial');
    const second = { ...first, id: 3 };
    const third = { ...first, id: 4 };

    const keys = itemKeys([first, other, second, third]);

    expect(keys.get(first)).toBe(itemIdentity(first));
    expect(keys.get(other)).toBe(itemIdentity(other));
    expect(keys.get(second)).toBe(`${itemIdentity(first)}#1`);
    expect(keys.get(third)).toBe(`${itemIdentity(first)}#2`);
    expect(new Set(keys.values()).size).toBe(4);
  });

  it('keeps a card, and focus on its action, when a newer run replaces every item id', () => {
    const items = [termItem(1, 'crm demo'), termItem(2, 'crm trial')];
    const show = renderReplaceable(items, cardList);
    const card = screen.getByRole('article', { name: 'Add "crm trial" as exact-match negative' });
    const action = within(card).getByRole('button');
    act(() => {
      action.focus();
    });
    expect(document.activeElement).toBe(action);

    show(items.map(nextRun));

    const replaced = screen.getByRole('article', {
      name: 'Add "crm trial" as exact-match negative',
    });
    expect(replaced).toBe(card);
    expect(replaced.id).toBe('item-1002');
    expect(within(replaced).getByRole('button')).toBe(action);
    expect(document.activeElement).toBe(action);
  });

  it('mounts a new card for a different recommendation, even when it reuses the id', () => {
    const show = renderReplaceable([termItem(1, 'crm demo')], cardList);
    const card = screen.getByRole('article');

    show([termItem(1, 'crm trial')]);

    const replaced = screen.getByRole('article', {
      name: 'Add "crm trial" as exact-match negative',
    });
    expect(replaced).not.toBe(card);
  });

  it("keeps a card of the frame's default body, and focus on its action, across a newer run", () => {
    const items = [termItem(1, 'crm demo'), termItem(2, 'crm trial')];
    const show = renderReplaceable(items, (shown) => (
      <SectionFrame
        section={makeSection('keywords', { groups: [makeGroup({ items: [...shown] })] })}
        renderItem={actionCard}
      />
    ));
    const action = within(
      screen.getByRole('article', { name: 'Add "crm demo" as exact-match negative' }),
    ).getByRole('button');
    act(() => {
      action.focus();
    });

    show(items.map(nextRun));

    expect(screen.getByRole('article', { name: 'Add "crm demo" as exact-match negative' }).id).toBe(
      'item-1001',
    );
    expect(action.isConnected).toBe(true);
    expect(document.activeElement).toBe(action);
  });

  it('keys two C0 cards of one subject apart, without a duplicate-key warning', () => {
    const consoleError = vi.spyOn(console, 'error');
    const show = renderReplaceable([discrepancyItem(1, 51), discrepancyItem(2, 52)], cardList);
    const second = screen.getByRole('article', { name: 'Resolve discrepancy 52' });

    show([discrepancyItem(11, 51), discrepancyItem(12, 52)]);

    expect(screen.getAllByRole('article').map((article) => article.id)).toEqual([
      'item-11',
      'item-12',
    ]);
    expect(screen.getByRole('article', { name: 'Resolve discrepancy 52' })).toBe(second);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('renders items that share an identity, or carry no entity id, each as its own card', () => {
    const consoleError = vi.spyOn(console, 'error');
    const shared = termItem(1, 'crm demo');
    render(
      cardList([
        shared,
        { ...shared, id: 2, action: 'A second action on the same term' },
        unidentifiedItem(7, null),
        unidentifiedItem(8, null),
      ]),
    );

    expect(screen.getAllByRole('article').map((article) => article.id)).toEqual([
      'item-1',
      'item-2',
      'item-7',
      'item-8',
    ]);
    expect(consoleError).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------------------------
// Confirmations of items hidden on the page
// ---------------------------------------------------------------------------------------------

describe('confirmations of items hidden on the page', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** A card as `RecommendationCard` anchors it, at `item-{id}`, holding one action. */
  function actionCard(item: Item): ReactNode {
    return (
      <article id={itemAnchorId(item.id)} aria-label={item.action}>
        <Button>{`Mark done: ${item.action}`}</Button>
      </article>
    );
  }

  /**
   * A C1 item on a campaign of its own, so every item of a test has its own recommendation
   * identity, with the overlay fields given and, with `held`, held by the run.
   */
  function campaignItem(
    id: number,
    action: string,
    overlay: Partial<ItemOverlay> = {},
    held = false,
  ): Item {
    return makeItem({
      id,
      rank: id,
      rule_id: 'C1',
      action,
      held,
      held_reason: held ? 'Resolve discrepancy first' : null,
      entity: {
        source: 'google',
        type: 'campaign',
        id: `6100${String(id)}`,
        name: `Campaign ${String(id)}`,
      },
      current: makeItemOverlay(overlay),
    });
  }

  /** An item computed from data that has since been replaced, on a campaign of its own. */
  function replacedItem(id: number, action: string): Item {
    return campaignItem(id, action, {
      historical: true,
      held_now: true,
      held_reason_now: 'Computed from data that has since been replaced',
      data_status_now: 'live',
    });
  }

  /** The item as a refetch shows it after a dismissal: hidden by feedback since the run. */
  function dismissed(item: Item): Item {
    return { ...item, current: { ...item.current, hidden_by: 'dismiss' } };
  }

  /** The confirmation a card records for the dismissal of `item`. */
  function dismissal(item: Item): string {
    return `Dismissed: ${item.action} (${item.entity.name ?? ''})`;
  }

  /** The confirmations of dismissing `items`, keyed as the provider keys them. */
  function confirmationsOf(...items: Item[]): Map<string, string> {
    return new Map(items.map((item) => [itemIdentity(item), dismissal(item)]));
  }

  /** Whether the elements stand in this document order. */
  function inOrder(...elements: Element[]): boolean {
    let previous: Element | undefined;
    for (const element of elements) {
      if (previous !== undefined && !follows(previous, element)) {
        return false;
      }
      previous = element;
    }
    return true;
  }

  /**
   * Asserts the confirmation of `item` is plain content: no live region, no alert, no control
   * and no Tab stop. Its one focus target, `tabindex="-1"`, carries the item's recommendation
   * identity, by which the card that recorded the feedback moves focus to it.
   */
  function expectPlainContent(confirmation: Element, item: Item): void {
    expect(confirmation.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull();
    expect(confirmation.closest('button, a')).toBeNull();
    const target = confirmation.closest('[tabindex]');
    expect(target?.getAttribute('tabindex')).toBe('-1');
    expect(target?.getAttribute('data-feedback-confirmation')).toBe(itemIdentity(item));
    expect(target?.parentElement?.closest('[tabindex]') ?? null).toBeNull();
  }

  /** Records the dismissal of `item` in the nearest provider when pressed, as a card does. */
  function Recorder({ item }: { item: Item }): ReactNode {
    const record = useRecordFeedbackConfirmation();
    return (
      <Button
        onClick={() => {
          record?.(itemIdentity(item), dismissal(item));
        }}
      >
        {`Record ${item.action}`}
      </Button>
    );
  }

  /**
   * Renders the frame over `initial` inside an `ActionOutcomeProvider`, beside a recorder for
   * each of `recorded`, and returns `show`, which hands the frame the section as a refetch does.
   */
  function renderOnPage(
    initial: Section,
    recorded: readonly Item[],
    props: Partial<SectionFrameProps> = {},
  ): (next: Section) => void {
    const control: { show?: (next: Section) => void } = {};
    function Host(): ReactNode {
      const [section, setSection] = useState(initial);
      control.show = setSection;
      return (
        <ActionOutcomeProvider>
          {recorded.map((item) => (
            <Recorder key={item.id} item={item} />
          ))}
          <SectionFrame section={section} renderItem={actionCard} {...props} />
        </ActionOutcomeProvider>
      );
    }
    render(<Host />);
    return (next) => {
      act(() => {
        control.show?.(next);
      });
    };
  }

  /** Presses the recorder of `item`, as its card does once its feedback is recorded. */
  function record(item: Item): void {
    fireEvent.click(screen.getByRole('button', { name: `Record ${item.action}` }));
  }

  it('confirms only an item feedback hides now, with a confirmation recorded on the page', () => {
    const item = campaignItem(1, 'Pause');
    const confirmations = confirmationsOf(item);

    expect(isConfirmed(dismissed(item), confirmations)).toBe(true);
    // Shown again by a newer run, or never hidden: the card shows, not its confirmation.
    expect(isConfirmed(item, confirmations)).toBe(false);
    // Hidden by feedback given elsewhere, such as another tab: nothing to confirm here.
    expect(isConfirmed(dismissed(campaignItem(2, 'Pause')), confirmations)).toBe(false);
    expect(isConfirmed(dismissed(item), new Map())).toBe(false);
  });

  it('places each confirmed item in the list it stood in, at its API position, and leaves the partition as it was', () => {
    const first = campaignItem(1, 'Visible first');
    const confirmedVisible = dismissed(campaignItem(2, 'Dismissed here'));
    const elsewhere = dismissed(campaignItem(3, 'Dismissed elsewhere'));
    const confirmedReplaced = dismissed(replacedItem(4, 'Replaced and dismissed here'));
    const held = campaignItem(5, 'Held', {}, true);
    const confirmedHeld = dismissed(campaignItem(6, 'Held and dismissed here', {}, true));
    const last = campaignItem(7, 'Visible last');
    const group = makeGroup({
      key: 'pause',
      title: 'Pause',
      items: [first, confirmedVisible, elsewhere, confirmedReplaced, held, confirmedHeld, last],
    });
    const confirmations = confirmationsOf(confirmedVisible, confirmedReplaced, confirmedHeld);

    expect(placeConfirmations(group, confirmations)).toEqual({
      visible: [first, confirmedVisible, last],
      historical: [confirmedReplaced],
      held: [held, confirmedHeld],
      confirmed: 3,
    });
    // Every count still counts all four hidden items, the confirmed ones included.
    expect(partitionGroup(group)).toEqual({
      visible: [first, last],
      historical: [],
      held: [held],
      hiddenCount: 4,
    });
    expect(placeConfirmations(group, new Map())).toEqual({
      visible: [first, last],
      historical: [],
      held: [held],
      confirmed: 0,
    });
  });

  it('renders each confirmation where its card stood in a card list, as plain content', () => {
    const leading = dismissed(campaignItem(1, 'Leading'));
    const second = campaignItem(2, 'Second');
    const third = dismissed(campaignItem(3, 'Third'));
    const fourth = dismissed(campaignItem(4, 'Fourth'));
    const fifth = campaignItem(5, 'Fifth');
    const elsewhere = dismissed(campaignItem(6, 'Elsewhere'));
    render(
      <CardList
        items={[leading, second, third, fourth, fifth, elsewhere]}
        renderCard={actionCard}
        confirmations={confirmationsOf(leading, third, fourth)}
      />,
    );

    const places = [
      screen.getByText(dismissal(leading)),
      screen.getByRole('article', { name: 'Second' }),
      screen.getByText(dismissal(third)),
      screen.getByText(dismissal(fourth)),
      screen.getByRole('article', { name: 'Fifth' }),
    ];
    expect(inOrder(...places)).toBe(true);
    for (const confirmed of [leading, third, fourth]) {
      expectPlainContent(screen.getByText(dismissal(confirmed)), confirmed);
    }
    // A hidden item without a confirmation on this page shows nothing, and no hidden card renders.
    expect(screen.queryByText(dismissal(elsewhere))).toBeNull();
    expect(screen.getAllByRole('article').map((card) => card.getAttribute('aria-label'))).toEqual([
      'Second',
      'Fifth',
    ]);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('never counts a confirmation as an action of a batched list', () => {
    const cards = Array.from({ length: CARD_BATCH_SIZE + 1 }, (_, index) =>
      campaignItem(index + 1, `Action ${String(index + 1)}`),
    );
    const hidden = dismissed(campaignItem(100, 'Dismissed here'));
    render(
      <CardList
        items={[hidden, ...cards]}
        renderCard={actionCard}
        confirmations={confirmationsOf(hidden)}
      />,
    );

    expect(screen.getByRole('status').textContent).toBe(
      `Showing ${String(CARD_BATCH_SIZE)} of ${String(CARD_BATCH_SIZE + 1)} actions`,
    );
    expect(screen.getAllByRole('article')).toHaveLength(CARD_BATCH_SIZE);
    expect(screen.getByText(dismissal(hidden))).toBeTruthy();
  });

  it('renders a list of confirmations alone, with no batch status', () => {
    const one = dismissed(campaignItem(1, 'One'));
    const two = dismissed(campaignItem(2, 'Two'));
    render(
      <CardList
        items={[one, two]}
        renderCard={actionCard}
        confirmations={confirmationsOf(one, two)}
      />,
    );

    expect(inOrder(screen.getByText(dismissal(one)), screen.getByText(dismissal(two)))).toBe(true);
    expect(screen.queryByRole('article')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('keeps the card before a confirmation mounted, and focus on its action, when its neighbour turns into one', () => {
    const first = campaignItem(1, 'First');
    const second = campaignItem(2, 'Second');
    const control: { show?: (next: readonly Item[]) => void } = {};
    function Host(): ReactNode {
      const [items, setItems] = useState<readonly Item[]>([first, second]);
      control.show = setItems;
      return (
        <CardList
          items={items}
          renderCard={actionCard}
          confirmations={confirmationsOf(dismissed(second))}
        />
      );
    }
    render(<Host />);
    const card = screen.getByRole('article', { name: 'First' });
    const action = within(card).getByRole('button');
    act(() => {
      action.focus();
    });
    const consoleError = vi.spyOn(console, 'error');

    act(() => {
      control.show?.([first, dismissed(second)]);
    });

    expect(screen.getByRole('article', { name: 'First' })).toBe(card);
    expect(document.activeElement).toBe(action);
    expect(follows(card, screen.getByText(dismissal(second)))).toBe(true);
    expect(screen.queryByRole('article', { name: 'Second' })).toBeNull();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('renders the confirmations of confirmed items alone for a table, or nothing', () => {
    const shown = campaignItem(1, 'Shown');
    const hidden = dismissed(campaignItem(2, 'Hidden here'));
    const elsewhere = dismissed(campaignItem(3, 'Hidden elsewhere'));
    const confirmations = confirmationsOf(shown, hidden);

    const { container, unmount } = render(
      <ConfirmedItems items={[shown, hidden, elsewhere]} confirmations={confirmations} />,
    );
    expect(screen.getByText(dismissal(hidden))).toBeTruthy();
    // A visible item is its own row; a confirmation recorded for it shows nothing here.
    expect(screen.queryByText(dismissal(shown))).toBeNull();
    expect(screen.queryByText(dismissal(elsewhere))).toBeNull();
    unmount();

    const { container: empty } = render(
      <ConfirmedItems items={[shown, elsewhere]} confirmations={confirmations} />,
    );
    expect(empty.childElementCount).toBe(0);
    expect(container.childElementCount).toBe(0);
  });

  it('keeps the region of a group whose every item was hidden on the page, with the confirmation in place of the empty state', () => {
    const c1 = campaignItem(1, 'Pause Generic CRM');
    const launch = campaignItem(2, 'Launch Partner Search');
    const show = renderOnPage(
      makeSection('campaigns', {
        groups: [
          makeGroup({ key: 'pause', title: 'Pause', items: [c1] }),
          makeGroup({ key: 'launch', title: 'Launch ideas', items: [launch] }),
        ],
      }),
      [c1, launch],
    );

    record(c1);
    // Recorded before the refetch hides it: the card still shows, and no confirmation.
    expect(screen.getByRole('article', { name: c1.action })).toBeTruthy();
    expect(screen.queryByText(dismissal(c1))).toBeNull();

    show(
      makeSection('campaigns', {
        groups: [
          makeGroup({ key: 'pause', title: 'Pause', items: [dismissed(c1)] }),
          makeGroup({ key: 'launch', title: 'Launch ideas', items: [launch] }),
        ],
      }),
    );

    const pause = screen.getByRole('region', { name: 'Pause' });
    expect(pause.id).toBe('group-campaigns-pause');
    const confirmation = within(pause).getByText(dismissal(c1));
    expectPlainContent(confirmation, c1);
    expect(within(pause).queryByRole('article')).toBeNull();
    expect(
      screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(['Pause', 'Launch ideas']);
    // The item still counts as hidden, and only the polite provider region is a status.
    expect(listLines('Not shown')).toEqual(['1 hidden since this run']);
    expect(screen.getAllByRole('status')).toHaveLength(1);
    expect(screen.queryByRole('alert')).toBeNull();

    // The section's last shown card hidden the same way: the confirmations replace the empty state.
    record(launch);
    show(
      makeSection('campaigns', {
        groups: [
          makeGroup({ key: 'pause', title: 'Pause', items: [dismissed(c1)] }),
          makeGroup({ key: 'launch', title: 'Launch ideas', items: [dismissed(launch)] }),
        ],
      }),
    );
    expect(emptyState()).toBeNull();
    expect(
      within(screen.getByRole('region', { name: 'Launch ideas' })).getByText(dismissal(launch)),
    ).toBeTruthy();
    expect(listLines('Not shown')).toEqual(['2 hidden since this run']);
  });

  it('shows nothing for a hidden item without a confirmation on the page, and the empty state when nothing else shows', () => {
    const c1 = campaignItem(1, 'Pause Generic CRM');
    renderOnPage(
      makeSection('campaigns', {
        groups: [makeGroup({ key: 'pause', title: 'Pause', items: [dismissed(c1)] })],
      }),
      [],
    );

    expect(screen.queryByRole('region', { name: 'Pause' })).toBeNull();
    expect(screen.queryByText(dismissal(c1))).toBeNull();
    expect(emptyTitle()).toBe(
      'No actions to show: 1 hidden by feedback since this run (dismissed, snoozed or done)',
    );
  });

  it('shows the confirmation in place of a replaced-data block or held disclosure the feedback emptied', () => {
    const visible = campaignItem(1, 'Visible');
    const replaced = replacedItem(2, 'Replaced');
    const held = campaignItem(3, 'Held', {}, true);
    const group = (items: Item[]) => makeGroup({ key: 'pause', title: 'Pause', items });
    const show = renderOnPage(
      makeSection('campaigns', { groups: [group([visible, replaced, held])] }),
      [replaced, held],
    );
    expect(screen.getByRole('group', { name: 'Computed from replaced data (1)' })).toBeTruthy();

    record(replaced);
    record(held);
    show(
      makeSection('campaigns', {
        groups: [group([visible, dismissed(replaced), dismissed(held)])],
      }),
    );

    const pause = screen.getByRole('region', { name: 'Pause' });
    const replacedConfirmation = within(pause).getByText(dismissal(replaced));
    const heldConfirmation = within(pause).getByText(dismissal(held));
    expect(
      inOrder(
        within(pause).getByRole('article', { name: 'Visible' }),
        replacedConfirmation,
        heldConfirmation,
      ),
    ).toBe(true);
    // Both blocks are empty now, so neither renders; the confirmations take their place.
    expect(
      within(pause).queryByRole('group', { name: /^Computed from replaced data/u }),
    ).toBeNull();
    expect(
      within(pause).queryByRole('button', { name: /^Held until discrepancies are resolved/u }),
    ).toBeNull();
    expectPlainContent(replacedConfirmation, replaced);
    expectPlainContent(heldConfirmation, held);
    expect(listLines('Not shown')).toEqual(['2 hidden since this run']);
  });

  it('keeps a confirmation inside the replaced-data block and the held disclosure that still hold cards', () => {
    const replaced = replacedItem(1, 'Replaced');
    const replacedNext = replacedItem(2, 'Replaced next');
    const held = campaignItem(3, 'Held', {}, true);
    const heldNext = campaignItem(4, 'Held next', {}, true);
    const group = (items: Item[]) => makeGroup({ key: 'pause', title: 'Pause', items });
    const show = renderOnPage(
      makeSection('campaigns', { groups: [group([replaced, replacedNext, held, heldNext])] }),
      [replaced, held],
    );

    record(replaced);
    record(held);
    show(
      makeSection('campaigns', {
        groups: [group([dismissed(replaced), replacedNext, dismissed(held), heldNext])],
      }),
    );

    const block = screen.getByRole('group', { name: 'Computed from replaced data (1)' });
    expect(
      inOrder(
        within(block).getByText(dismissal(replaced)),
        within(block).getByRole('article', { name: 'Replaced next' }),
      ),
    ).toBe(true);
    fireEvent.click(
      screen.getByRole('button', { name: 'Held until discrepancies are resolved (1)' }),
    );
    const heldRegion = document.getElementById('group-campaigns-pause-held');
    if (heldRegion === null) {
      throw new Error('the held region left the group');
    }
    expect(
      inOrder(
        within(heldRegion).getByText(dismissal(held)),
        within(heldRegion).getByRole('article', { name: 'Held next' }),
      ),
    ).toBe(true);
  });

  it('removes a confirmation once a rerun drops the item, and shows the card again once it is no longer hidden', () => {
    const c1 = campaignItem(1, 'Pause Generic CRM');
    const other = campaignItem(2, 'Pause Brand');
    const section = (items: Item[]) =>
      makeSection('campaigns', { groups: [makeGroup({ key: 'pause', title: 'Pause', items })] });
    const show = renderOnPage(section([c1, other]), [c1]);
    record(c1);
    show(section([dismissed(c1), other]));
    expect(screen.getByText(dismissal(c1))).toBeTruthy();

    // A rerun lists the recommendation unhidden, under a new id: the card, not the confirmation.
    show(section([{ ...c1, id: 1001 }, other]));
    expect(screen.queryByText(dismissal(c1))).toBeNull();
    expect(screen.getByRole('article', { name: c1.action }).id).toBe('item-1001');

    // A rerun that no longer lists it removes it entirely.
    show(section([dismissed(c1), other]));
    expect(screen.getByText(dismissal(c1))).toBeTruthy();
    show(section([other]));
    expect(screen.queryByText(dismissal(c1))).toBeNull();
    expect(screen.queryByRole('list', { name: 'Not shown' })).toBeNull();
  });

  it('hands renderGroup and renderGroups the placed lists and the confirmations', () => {
    const c1 = campaignItem(1, 'Pause Generic CRM');
    const other = campaignItem(2, 'Pause Brand');
    const renderGroup = vi.fn((_group: Group, parts: GroupParts) => (
      <CardList
        items={parts.placed.visible}
        renderCard={actionCard}
        confirmations={parts.confirmations}
      />
    ));
    const show = renderOnPage(
      makeSection('campaigns', {
        groups: [makeGroup({ key: 'pause', title: 'Pause', items: [c1, other] })],
      }),
      [c1],
      { renderGroup },
    );
    record(c1);
    show(
      makeSection('campaigns', {
        groups: [makeGroup({ key: 'pause', title: 'Pause', items: [dismissed(c1), other] })],
      }),
    );

    const parts = renderGroup.mock.lastCall?.[1];
    expect(parts?.visible.map((item) => item.id)).toEqual([2]);
    expect(parts?.hiddenCount).toBe(1);
    expect(parts?.placed.visible.map((item) => item.id)).toEqual([1, 2]);
    expect(parts?.placed.confirmed).toBe(1);
    expect(parts?.confirmations.get(itemIdentity(c1))).toBe(dismissal(c1));
    expect(
      inOrder(
        screen.getByText(dismissal(c1)),
        screen.getByRole('article', { name: other.action }),
      ),
    ).toBe(true);
  });
});
