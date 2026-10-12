/**
 * Unit tests for `B1Cards`, where a C2 "Scale" card finds the B1 card it links to
 * (AAP 0.5.4.4 C2: "'Scale', with a link to its B1 item, which carries the amount").
 *
 * They hold the module to its TSDoc contract:
 * - `indexB1Cards` keys the B1 cards the page renders by decision subject (`"{type}:{id}"`),
 *   across every section and group, keeps the first card of a repeated subject, reads a blank
 *   action as `null`, and leaves out B1 items without a subject, items of other rules and items
 *   feedback hides, while keeping one held until discrepancies are resolved (behind its held
 *   disclosure) and one computed from replaced data (shown openly);
 * - `useLinkedB1Card` resolves a C2 item's link inside a `B1CardsProvider` only, only for a link
 *   to B1, and rebuilds nothing for a page whose sections did not change.
 *
 * Assertions use Testing Library and Vitest's own matchers only.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReactElement } from 'react';

import type { Item, Section } from '../../api/types';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeGroup,
  makeItemOverlay,
  makeRecommendationsPage,
  makeSection,
} from '../../test/fixtures';
import { B1CardsProvider, indexB1Cards, useLinkedB1Card } from './B1Cards';
import type { B1Card } from './B1Cards';
import type { B1Link } from './evidenceExtra';

/** The default page's B1 item, "Raise daily budget from USD 200.00 to USD 240.00". */
function pageB1(): Item {
  return findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.b1);
}

/** A copy of the default page's B1 item with `overrides`. */
function b1With(overrides: Partial<Item>): Item {
  return { ...pageB1(), ...overrides };
}

/** A Budget section whose one group, "changes", holds `items`. */
function budgetWith(items: Item[]): Section {
  return makeSection('budget', {
    groups: [makeGroup({ key: 'changes', title: 'Budget changes', items })],
  });
}

/** The link a C2 "Scale" on "Pipeline Search — US", the default page's B1 subject, carries. */
const PIPELINE_SEARCH_LINK: B1Link = {
  ruleId: 'B1',
  subject: { type: 'campaign', id: String(FIXTURE_CAMPAIGN_IDS.pipelineSearch) },
};

describe('indexB1Cards', () => {
  it("indexes the default page's B1 card by its decision subject", () => {
    const b1 = pageB1();
    const index = indexB1Cards(makeRecommendationsPage().sections);
    expect([...index.entries()]).toEqual([
      [
        `campaign:${String(FIXTURE_CAMPAIGN_IDS.pipelineSearch)}`,
        { itemId: b1.id, action: b1.action },
      ],
    ]);
  });

  it('keeps the first card of a repeated subject, across groups and sections', () => {
    const first = b1With({ id: 9001 });
    const repeat = b1With({ id: 9002, action: 'Raise daily budget again' });
    const shared = b1With({
      id: 9003,
      decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
    });
    const elsewhere = makeSection('campaigns', {
      groups: [makeGroup({ key: 'scale', title: 'Scale', items: [repeat] })],
    });
    const index = indexB1Cards([budgetWith([first, shared]), elsewhere]);
    expect(index.get(`campaign:${String(FIXTURE_CAMPAIGN_IDS.pipelineSearch)}`)?.itemId).toBe(
      9001,
    );
    expect(index.get(`budget:${String(FIXTURE_BUDGET_IDS.search)}`)?.itemId).toBe(9003);
    expect(index.size).toBe(2);
  });

  it('leaves out other rules, B1 items without a subject and cards feedback hides', () => {
    const items: Item[] = [
      b1With({ id: 9101, rule_id: 'B3' }),
      b1With({ id: 9102, rule_id: 'b1' }),
      b1With({ id: 9103, decision_subject: null }),
      b1With({ id: 9104, current: makeItemOverlay({ hidden_by: 'done' }) }),
      // Hidden by feedback while held: counted, never rendered, so never linked.
      b1With({ id: 9105, held: true, current: makeItemOverlay({ hidden_by: 'snooze' }) }),
    ];
    expect(indexB1Cards([budgetWith(items)]).size).toBe(0);
  });

  it('keeps a card held until discrepancies are resolved, by the run or by the overlay now', () => {
    const key = `campaign:${String(FIXTURE_CAMPAIGN_IDS.pipelineSearch)}`;
    // Held by the run: rendered behind its group's held disclosure, which the link's hash opens.
    const frozen = indexB1Cards([budgetWith([b1With({ id: 9401, held: true })])]);
    expect([...frozen.entries()]).toEqual([[key, { itemId: 9401, action: pageB1().action }]]);
    // Held now by a discrepancy detected after the run.
    const heldNow = b1With({ id: 9402, current: makeItemOverlay({ held_now: true }) });
    expect(indexB1Cards([budgetWith([heldNow])]).get(key)?.itemId).toBe(9402);
    // A held card ahead of an open one for the same subject is the first card, so it is kept.
    const both = indexB1Cards([
      budgetWith([b1With({ id: 9403, held: true }), b1With({ id: 9404 })]),
    ]);
    expect(both.get(key)?.itemId).toBe(9403);
    expect(both.size).toBe(1);
  });

  it('keeps a card computed from replaced data, which the page shows openly', () => {
    const historical = b1With({
      id: 9201,
      current: makeItemOverlay({ historical: true, held_now: true }),
    });
    const index = indexB1Cards([budgetWith([historical])]);
    expect(index.get(`campaign:${String(FIXTURE_CAMPAIGN_IDS.pipelineSearch)}`)?.itemId).toBe(
      9201,
    );
  });

  it('reads a blank action as null and trims the rest', () => {
    const blank = indexB1Cards([budgetWith([b1With({ id: 9301, action: '   ' })])]);
    expect([...blank.values()]).toEqual([{ itemId: 9301, action: null }]);
    const padded = indexB1Cards([budgetWith([b1With({ id: 9302, action: '  Raise it  ' })])]);
    expect([...padded.values()]).toEqual([{ itemId: 9302, action: 'Raise it' }]);
  });

  it('returns an empty index for a page without sections or items', () => {
    expect(indexB1Cards([]).size).toBe(0);
    expect(indexB1Cards([budgetWith([])]).size).toBe(0);
  });
});

/** Shows what {@link useLinkedB1Card} resolves `link` to, as JSON. */
function Probe({ link }: { link: B1Link | null }): ReactElement {
  const card: B1Card | null = useLinkedB1Card(link);
  return <output>{JSON.stringify(card)}</output>;
}

describe('useLinkedB1Card', () => {
  it("resolves a link to the provider's B1 card for its subject", () => {
    const b1 = pageB1();
    render(
      <B1CardsProvider sections={makeRecommendationsPage().sections}>
        <Probe link={PIPELINE_SEARCH_LINK} />
      </B1CardsProvider>,
    );
    expect(JSON.parse(screen.getByRole('status').textContent)).toEqual({
      itemId: b1.id,
      action: b1.action,
    });
  });

  it('resolves a link to a held B1 card, and to nothing once feedback hides it', () => {
    const held = b1With({ id: 9501, held: true, current: makeItemOverlay({ held_now: true }) });
    const { unmount } = render(
      <B1CardsProvider sections={[budgetWith([held])]}>
        <Probe link={PIPELINE_SEARCH_LINK} />
      </B1CardsProvider>,
    );
    expect(JSON.parse(screen.getByRole('status').textContent)).toEqual({
      itemId: 9501,
      action: held.action,
    });
    unmount();

    const hidden: Item = {
      ...held,
      current: makeItemOverlay({ held_now: true, hidden_by: 'dismiss' }),
    };
    render(
      <B1CardsProvider sections={[budgetWith([hidden])]}>
        <Probe link={PIPELINE_SEARCH_LINK} />
      </B1CardsProvider>,
    );
    expect(screen.getByRole('status').textContent).toBe('null');
  });

  it('resolves nothing outside the provider, without a link, or for a link to another rule', () => {
    const sections = makeRecommendationsPage().sections;
    const { unmount } = render(<Probe link={PIPELINE_SEARCH_LINK} />);
    expect(screen.getByRole('status').textContent).toBe('null');
    unmount();

    const links: (B1Link | null)[] = [
      null,
      { ...PIPELINE_SEARCH_LINK, ruleId: 'B3' },
      { ruleId: 'B1', subject: { type: 'budget', id: PIPELINE_SEARCH_LINK.subject.id } },
      { ruleId: 'B1', subject: { type: 'campaign', id: '999999' } },
    ];
    for (const link of links) {
      const probe = render(
        <B1CardsProvider sections={sections}>
          <Probe link={link} />
        </B1CardsProvider>,
      );
      expect(screen.getByRole('status').textContent, JSON.stringify(link)).toBe('null');
      probe.unmount();
    }
  });
});
