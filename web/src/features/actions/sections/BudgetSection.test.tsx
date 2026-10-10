/**
 * Component tests for `BudgetSection`, the "Budget changes" section of `/actions` (AAP 0.5.5
 * Budget; AAP 0.5.4.5 B1–B4; AAP 0.5.4.2 G1 and "Presenting reallocations"; FR-6).
 *
 * They hold the section to its TSDoc contract on the default fixture page and on cards built
 * here in the backend's shape (`analysis/budget.py`, `analysis/metrics.py` gates and
 * `analysis/precedence.py` reallocations):
 * - the items in API rank order, then "Patterns withheld for sample size" with each pattern's
 *   observed and required sample, then the suppression counts such as the increases G1 blocked;
 * - utilization beside efficiency, each a named group in one two-column grid, with the spending
 *   and predicate days against what the gate requires ("n/a" for a lifetime budget, "—" for a
 *   cost with no outcome);
 * - the "Budget days" table of every day count against its requirement, the stricter of two
 *   checks on one count, and "—" where no gate requires one;
 * - outcomes, CRM Qualified Leads only when linked, the daily budget and the proposed amount;
 * - every member campaign of a shared budget with its member days;
 * - the B1 funding sentence and each B3 allocation as funding for a named B1, linked to that
 *   card when the evidence names it, with the unallocated part as a plain decrease, including
 *   the AAP's two-donor allocation of 300 and 100 against a 400 increase;
 * - held cards collapsed behind the held disclosure, and the data-state flags;
 * - each card's amounts in its own account currency, with no total summed in the browser
 *   (AAP 0.1.1 "never converted or summed across currencies").
 *
 * The section fetches nothing: every test stubs the network with an empty `mockApi`, which
 * fails the test on any request.
 */
import { mockApi } from '../../../test/mockApi';
import { renderWithProviders } from '../../../test/render';
import { fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Baseline, Flag, Item, Metric, Section } from '../../../api/types';
import { formatMoney } from '../../../lib/format';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeEvidence,
  makeFlag,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
  makeWithheld,
} from '../../../test/fixtures';
import { BudgetSection } from './BudgetSection';

// ---------------------------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------------------------

/** One evidence metric as the backend serialises it (`unavailable` exactly when `null`). */
function metric(
  key: string,
  label: string,
  value: Metric['value'],
  unit: string | null,
  currency: string | null = null,
): Metric {
  return { key, label, value, unit, currency, floor: null, unavailable: value === null };
}

/** One baseline as the backend serialises it. */
function baseline(key: string, label: string, value: Baseline['value']): Baseline {
  return { key, label, value };
}

/** A USD amount metric. */
function usd(key: string, label: string, value: string | null): Metric {
  return metric(key, label, value, 'currency', 'USD');
}

/** A day-count metric. */
function days(key: string, label: string, value: number): Metric {
  return metric(key, label, value, 'days');
}

/** `{check: {observed, required}}`, as `budget.budget_evidence` writes `extra.gate`. */
type Gate = Record<string, { observed: number; required: number }>;

/** The default page's Budget section. */
function defaultBudget(): Section {
  const section = makeRecommendationsPage().sections.find((entry) => entry.key === 'budget');
  if (section === undefined) {
    throw new Error('the default fixture page has no budget section');
  }
  return section;
}

/** The default page's item `id`. */
function pageItem(id: Item['id']): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** `item` with `patch` merged over its `evidence.extra` (shallow). */
function withExtra(item: Item, patch: Record<string, unknown>): Item {
  return {
    ...item,
    evidence: makeEvidence({ ...item.evidence, extra: { ...item.evidence.extra, ...patch } }),
  };
}

/** The default B1 with the backend's cap-constrained gate over 26 known-budget days. */
function backendB1(): Item {
  // metrics.cap_constrained: capped_share requires ⌈0.7 × 26⌉ = 19 whole days, which binds
  // over budget.min_capped_days (5).
  const gate: Gate = {
    known_budget_days: { observed: 26, required: 7 },
    spending_days: { observed: 26, required: 5 },
    capped_days: { observed: 22, required: 5 },
    capped_share: { observed: 22, required: 19 },
  };
  return withExtra(pageItem(FIXTURE_ITEM_IDS.b1), { gate });
}

/** The default B3 with the backend's under-utilized gate over 24 budget-active days. */
function backendB3(): Item {
  // metrics.under_utilized: under_utilized_days requires ⌈0.7 × 24⌉ = 17 whole days.
  const gate: Gate = {
    known_budget_days: { observed: 24, required: 7 },
    spending_days: { observed: 24, required: 5 },
    under_utilized_days: { observed: 20, required: 17 },
  };
  return withExtra(pageItem(FIXTURE_ITEM_IDS.b3), { gate });
}

/** A budget card on a Google campaign with these metrics and `extra`. */
function budgetItem(spec: {
  id: number;
  rule_id: string;
  action: string;
  metrics: Metric[];
  baselines?: Baseline[];
  extra: Record<string, unknown> | null;
  flags?: Flag[];
  decision_subject?: Item['decision_subject'];
  current?: Item['current'];
}): Item {
  return makeItem({
    id: spec.id,
    rank: 1,
    rule_id: spec.rule_id,
    action: spec.action,
    reason: `${spec.rule_id} reason.`,
    entity: {
      source: 'google',
      type: 'campaign',
      id: String(spec.id),
      name: `Campaign ${String(spec.id)}`,
    },
    evidence: makeEvidence({
      metrics: spec.metrics,
      baselines: spec.baselines ?? [],
      extra: spec.extra,
    }),
    flags: spec.flags ?? [makeFlag('simulated')],
    decision_subject: spec.decision_subject ?? null,
    current: spec.current ?? makeItemOverlay(),
  });
}

/** A Budget section with one "Budget changes" group holding `items`. */
function budgetWith(items: Item[], overrides: Partial<Section> = {}): Section {
  return makeSection('budget', {
    groups: [makeGroup({ key: 'changes', title: 'Budget changes', items })],
    ...overrides,
  });
}

/** Renders the section inside the providers, with a network stub that allows no request. */
async function renderBudget(section: Section) {
  const api = mockApi({});
  const result = await renderWithProviders(
    <BudgetSection section={section} run={makeRun()} overlay={makeOverlay()} />,
  );
  return { ...result, api };
}

/** The card (`<article>`) named by its action. */
function card(action: string): HTMLElement {
  return screen.getByRole('article', { name: action });
}

/** The text of every `<li>` of the list with this accessible name inside `scope`. */
function listLines(scope: HTMLElement, name: string): string[] {
  return within(within(scope).getByRole('list', { name }))
    .getAllByRole('listitem')
    .map((entry) => entry.textContent);
}

/** The labelled group (`role="group"`) with this name inside `scope`. */
function namedGroup(scope: HTMLElement, name: string): HTMLElement {
  return within(scope).getByRole('group', { name });
}

/** The lines of the unnamed field list inside the named group. */
function groupFields(scope: HTMLElement, name: string): string[] {
  const group = namedGroup(scope, name);
  return within(within(group).getByRole('list'))
    .getAllByRole('listitem')
    .map((entry) => entry.textContent);
}

/** The header texts and every body row's cell texts of the table with this caption. */
function tableOf(scope: HTMLElement, caption: string): { headers: string[]; rows: string[][] } {
  const table = within(scope).getByRole('table', { name: caption });
  const headers = within(table)
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent);
  const rows = within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent));
  return { headers, rows };
}

/** Whether `later` comes after `earlier` in document order. */
function follows(earlier: Element, later: Element): boolean {
  return (earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

const B1_ACTION = 'Raise daily budget from USD 200.00 to USD 240.00';
const B3_ACTION = 'Lower daily budget to USD 110.00';

// ---------------------------------------------------------------------------------------------
// Section layout
// ---------------------------------------------------------------------------------------------

describe('BudgetSection layout', () => {
  it('lists items in rank order, then withheld patterns, then G1 blocks', async () => {
    const { api } = await renderBudget(defaultBudget());

    const section = document.getElementById('section-budget');
    expect(section?.tagName).toBe('SECTION');
    expect(
      within(section as HTMLElement).getByRole('heading', { level: 2, name: 'Budget changes' }),
    ).toBeTruthy();
    const group = document.getElementById('group-budget-changes');
    expect(within(group as HTMLElement).getByRole('heading', { level: 3 }).textContent).toBe(
      'Budget changes',
    );

    // Ranked by window spend: the B1 first, then the B3, each heading at level 4.
    const articles = [...(group?.querySelectorAll('article') ?? [])];
    expect(articles.map((article) => article.id)).toEqual([
      `item-${String(FIXTURE_ITEM_IDS.b1)}`,
      `item-${String(FIXTURE_ITEM_IDS.b3)}`,
    ]);
    expect(screen.getByRole('heading', { level: 4, name: B1_ACTION })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 4, name: B3_ACTION })).toBeTruthy();

    // Items, then "Patterns withheld for sample size" (AAP 0.5.5), with the sample sizes.
    const withheld = screen.getByRole('list', { name: 'Patterns withheld for sample size' });
    expect(screen.getByText('Patterns withheld for sample size').tagName).toBe('STRONG');
    expect(listLines(section as HTMLElement, 'Patterns withheld for sample size')).toEqual([
      'B4 · Generic CRM — US: 3 weekend days of 4 required — 3 / 4 weekend days',
    ]);
    expect(follows(group as HTMLElement, withheld)).toBe(true);

    // The suppression count the run stored for the G1 guard.
    expect(listLines(section as HTMLElement, 'Not shown')).toEqual([
      '1 spend increase blocked by the high-utilization guard',
    ]);
    expect(follows(withheld, screen.getByRole('list', { name: 'Not shown' }))).toBe(true);

    expect(screen.queryByRole('alert')).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it('states why the section is empty: the G1 count and the withheld patterns', async () => {
    await renderBudget(
      makeSection('budget', {
        groups: [makeGroup({ key: 'changes', title: 'Budget changes', items: [] })],
        withheld: [
          makeWithheld({
            rule_id: 'B2',
            entity: {
              source: 'google',
              type: 'campaign',
              id: '21000302',
              name: 'Generic CRM — US',
            },
            reason: 'spending days 4 of 5 required',
            sample: { observed: 4, required: 5, unit: 'spending days' },
          }),
        ],
        suppressed: { g1_blocked: 2 },
      }),
    );

    const reason = document.querySelector('.ds-emptystate__reason');
    expect(document.querySelector('.ds-emptystate__title')?.textContent).toBe(
      'No actions in this section',
    );
    expect([...(reason?.querySelectorAll('span') ?? [])].map((line) => line.textContent)).toEqual([
      '2 spend increases blocked by the high-utilization guard',
      'B2 · Generic CRM — US: spending days 4 of 5 required — 4 / 5 spending days',
    ]);
    expect(
      listLines(
        document.getElementById('section-budget') as HTMLElement,
        'Patterns withheld for sample size',
      ),
    ).toEqual(['B2 · Generic CRM — US: spending days 4 of 5 required — 4 / 5 spending days']);
    expect(document.getElementById('group-budget-changes')).toBeNull();
  });

  it('collapses a card held by a discrepancy behind the held disclosure', async () => {
    const b1 = pageItem(FIXTURE_ITEM_IDS.b1);
    const held: Item = {
      ...b1,
      current: makeItemOverlay({ held_now: true, held_reason_now: 'Resolve discrepancy first' }),
    };
    await renderBudget(budgetWith([held, pageItem(FIXTURE_ITEM_IDS.b3)]));

    const toggle = screen.getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    const region = document.getElementById('group-budget-changes-held');
    expect(region?.hidden).toBe(true);
    // The held card mounts only once the disclosure is first opened.
    expect(region?.querySelector('article')).toBeNull();

    fireEvent.click(toggle);
    expect(region?.hidden).toBe(false);
    expect(region?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.b1)}`)).not.toBeNull();
    expect(region?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.b3)}`)).toBeNull();
    // Held by the overlay alone, with no blocker recorded or listed here: the list link.
    expect(
      within(card(B1_ACTION))
        .getByRole('link', { name: 'View open discrepancies' })
        .getAttribute('href'),
    ).toBe('#group-campaigns-discrepancies');
  });

  it('shows a card computed from replaced data openly after the current ones, outside the held disclosure', async () => {
    const historical: Item = {
      ...pageItem(FIXTURE_ITEM_IDS.b1),
      current: makeItemOverlay({
        historical: true,
        held_now: true,
        held_reason_now: 'Computed from data that has since been replaced',
        data_status_now: 'live',
      }),
    };
    await renderBudget(budgetWith([historical, pageItem(FIXTURE_ITEM_IDS.b3)]));

    // Replaced data is no discrepancy hold: no disclosure, and the card is readable at once.
    expect(
      screen.queryByRole('button', { name: /^Held until discrepancies are resolved/u }),
    ).toBeNull();
    const block = screen.getByRole('group', { name: 'Computed from replaced data (1)' });
    expect(block.id).toBe('group-budget-changes-historical');
    const replaced = within(block).getByRole('article', { name: B1_ACTION });
    expect(within(replaced).getByText('Computed from replaced data')).toBeTruthy();
    expect(
      within(replaced).getByRole('button', { name: 'Record decision' }).hasAttribute('disabled'),
    ).toBe(true);
    expect(
      card(B3_ACTION).compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------------------------
// B1: utilization, efficiency, days, details and funding
// ---------------------------------------------------------------------------------------------

describe('B1 increase card', () => {
  it('shows utilization and efficiency as adjacent named groups of one grid', async () => {
    await renderBudget(budgetWith([backendB1()]));

    const b1 = card(B1_ACTION);
    const grid = namedGroup(b1, 'Utilization and efficiency');
    expect(grid.className).toContain('ds-grid');
    const cells = [...grid.children];
    expect(cells.map((cell) => cell.getAttribute('aria-label'))).toEqual([
      'Utilization',
      'Efficiency',
    ]);

    expect(groupFields(b1, 'Utilization')).toEqual([
      'Window utilization (spend vs cap): 99%',
      // The stricter of capped_days (5) and capped_share (19) binds.
      'Days (observed / required): Spending days 26 / 5 · Capped days 22 / 19',
    ]);
    expect(groupFields(b1, 'Efficiency')).toEqual([
      'Cost per conversion: $82.37',
      'Account cost per conversion (same source and objective): $95.00',
    ]);
  });

  it('lists every day count against its requirement in the Budget days table', async () => {
    await renderBudget(budgetWith([backendB1()]));

    const daysTable = tableOf(card(B1_ACTION), 'Budget days');
    expect(daysTable.headers).toEqual(['Day type', 'Observed', 'Required']);
    expect(daysTable.rows).toEqual([
      ['Known-budget days', '26', '7'],
      ['Spending days', '26', '5'],
      ['Capped days', '22', '19'],
      ['Known idle days', '0', '—'],
      ['Changed-intraday days (unknown budget)', '1', '—'],
      ['Unobserved days (unknown budget)', '1', '—'],
    ]);
  });

  it('shows outcomes, daily and proposed budget, no CRM QL when unlinked', async () => {
    await renderBudget(budgetWith([backendB1()]));

    expect(listLines(card(B1_ACTION), 'Budget details')).toEqual([
      'Window conversions: 62.5',
      'Latest effective daily budget: $200.00',
      'Proposed daily budget: $240.00',
    ]);
  });

  it("shows the backend's funding sentence, and no member or reallocation list", async () => {
    await renderBudget(budgetWith([backendB1()]));

    const b1 = card(B1_ACTION);
    expect(
      within(b1).getByText(
        'Raise by USD 40.00: USD 30.00 funded by reallocation from Display Shared Budget, ' +
          'USD 10.00 from new budget',
      ).tagName,
    ).toBe('DIV');
    expect(within(b1).queryByRole('list', { name: 'Shared budget members' })).toBeNull();
    expect(
      within(b1).queryByRole('list', { name: 'Reallocations to recommended increases' }),
    ).toBeNull();
    expect(within(b1).queryByText(/^Plain decrease/u)).toBeNull();
    // B1 carries a decision subject, so the card records a decision rather than "Mark done".
    expect(within(b1).getByRole('button', { name: 'Record decision' })).toBeTruthy();
    expect(within(b1).queryByRole('button', { name: 'Mark done' })).toBeNull();
  });

  it('builds the funding sentence from the amounts, each donor once', async () => {
    const b1 = withExtra(backendB1(), {
      funding: null,
      increase: '400.00',
      funded_by_reallocation: '400.00',
      from_new_budget: '0.00',
      allocations: [
        { donor: { type: 'campaign', id: 31 }, donor_name: 'Donor A', amount: '300.00' },
        { donor: { type: 'budget', id: 32 }, donor_name: 'Donor B', amount: '60.00' },
        { donor: { type: 'budget', id: 32 }, donor_name: ' Donor B ', amount: '40.00' },
      ],
    });
    await renderBudget(budgetWith([b1]));

    expect(
      within(card(B1_ACTION)).getByText(
        'Raise by $400.00: $400.00 funded by reallocation from Donor A, Donor B, ' +
          '$0.00 from new budget',
      ),
    ).toBeTruthy();
  });

  it('leaves out the "from" clause when no donor funds the increase', async () => {
    const b1 = withExtra(backendB1(), {
      funding: null,
      funded_by_reallocation: '0.00',
      from_new_budget: '40.00',
      allocations: [],
    });
    await renderBudget(budgetWith([b1]));

    expect(
      within(card(B1_ACTION)).getByText(
        'Raise by $40.00: $0.00 funded by reallocation, $40.00 from new budget',
      ),
    ).toBeTruthy();
  });

  it.each([
    ['the new-budget part is missing', { funding: null, from_new_budget: null }],
    ['the reallocated part is missing', { funding: null, funded_by_reallocation: null }],
    ['the increase is missing', { funding: 'Raise by USD 40.00', increase: null }],
  ])('shows no funding line when %s, never a partial split', async (_label, patch) => {
    await renderBudget(budgetWith([withExtra(backendB1(), patch)]));
    expect(within(card(B1_ACTION)).queryByText(/^Raise by/u)).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// B3: shared budget, allocations and the plain decrease
// ---------------------------------------------------------------------------------------------

describe('B3 under-utilized card', () => {
  it('shows utilization against the under-utilized gate and efficiency beside it', async () => {
    await renderBudget(budgetWith([backendB3()]));

    const b3 = card(B3_ACTION);
    expect(groupFields(b3, 'Utilization')).toEqual([
      'Window utilization (spend vs cap): 44.3%',
      'Days (observed / required): Spending days 24 / 5 · Under-utilized days 20 / 17',
    ]);
    expect(groupFields(b3, 'Efficiency')).toEqual([
      'Cost per conversion: $48.00',
      'Account cost per conversion (same source and objective): $95.00',
    ]);
    expect(tableOf(b3, 'Budget days').rows).toEqual([
      ['Budget-active days', '24', '7'],
      ['Spending days', '24', '5'],
      ['Under-utilized days', '20', '17'],
      ['Known idle days', '0', '—'],
      ['Changed-intraday days (unknown budget)', '0', '—'],
      ['Unobserved days (unknown budget)', '4', '—'],
    ]);
    expect(listLines(b3, 'Budget details')).toEqual([
      'Window conversions: 31',
      'Latest effective daily budget: $140.00',
      'Proposed daily budget: $110.00',
    ]);
  });

  it('lists every member campaign of the shared budget with its member days', async () => {
    await renderBudget(budgetWith([backendB3()]));

    const b3 = card(B3_ACTION);
    expect(within(b3).getByText('Shared budget members').tagName).toBe('SPAN');
    expect(listLines(b3, 'Shared budget members')).toEqual([
      'Display Prospecting — US · 24 member days',
      'Display Retargeting — US · 18 member days',
    ]);
    expect(listLines(b3, 'Flags')).toContain(makeFlag('shared_budget').message);
    // The shared budget's decision is written on the budget subject.
    expect(within(b3).getByRole('button', { name: 'Record decision' })).toBeTruthy();
  });

  it('shows each allocation as funding for the named B1', async () => {
    await renderBudget(budgetWith([backendB3()]));

    const b3 = card(B3_ACTION);
    expect(listLines(b3, 'Reallocations to recommended increases')).toEqual([
      "Move USD 30.00 to fund Pipeline Search — US's recommended increase (B1)",
    ]);
    // The backend names no receiver card, so the line carries no link.
    const list = within(b3).getByRole('list', { name: 'Reallocations to recommended increases' });
    expect(within(list).queryByRole('link')).toBeNull();
  });

  it('links the receiver to its B1 card, keeping the line as rendered', async () => {
    const b3 = withExtra(backendB3(), {
      allocations: [
        {
          receiver: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineSearch },
          receiver_name: 'Pipeline Search — US',
          receiver_item_id: FIXTURE_ITEM_IDS.b1,
          amount: '30.00',
          text: "Move USD 30.00 to fund Pipeline Search — US's recommended increase (B1)",
        },
      ],
    });
    await renderBudget(budgetWith([backendB1(), b3]));

    const list = within(card(B3_ACTION)).getByRole('list', {
      name: 'Reallocations to recommended increases',
    });
    expect(within(list).getByRole('listitem').textContent).toBe(
      "Move USD 30.00 to fund Pipeline Search — US's recommended increase (B1)",
    );
    const link = within(list).getByRole('link', { name: 'Pipeline Search — US' });
    expect(link.getAttribute('href')).toBe(`#item-${String(FIXTURE_ITEM_IDS.b1)}`);
    // The link lands on the receiver's own card.
    expect(document.getElementById(`item-${String(FIXTURE_ITEM_IDS.b1)}`)).toBe(card(B1_ACTION));
  });

  it('rebuilds a linked line when it lacks the name or is missing', async () => {
    const b3 = withExtra(backendB3(), {
      allocations: [
        {
          receiver: { type: 'campaign', id: 51 },
          receiver_name: 'Receiver — Search',
          receiver_item_id: 9001,
          amount: '20.00',
          text: 'Move USD 20.00 to fund the recommended increase (B1)',
        },
        {
          receiver: { type: 'budget', id: 52 },
          receiver_name: 'Receiver — Shared',
          receiver_item_id: 9002,
          amount: '10.00',
        },
      ],
    });
    await renderBudget(budgetWith([b3]));

    const list = within(card(B3_ACTION)).getByRole('list', {
      name: 'Reallocations to recommended increases',
    });
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent),
    ).toEqual([
      "Move $20.00 to fund Receiver — Search's recommended increase (B1)",
      "Move $10.00 to fund Receiver — Shared's recommended increase (B1)",
    ]);
    expect(
      within(list)
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Receiver — Search', '#item-9001'],
      ['Receiver — Shared', '#item-9002'],
    ]);
  });

  it('builds an unlinked line from the allocation when the backend gave no line', async () => {
    const b3 = withExtra(backendB3(), {
      allocations: [
        {
          receiver: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineSearch },
          receiver_name: 'Pipeline Search — US',
          amount: '30.00',
        },
      ],
    });
    await renderBudget(budgetWith([b3]));

    expect(listLines(card(B3_ACTION), 'Reallocations to recommended increases')).toEqual([
      "Move $30.00 to fund Pipeline Search — US's recommended increase (B1)",
    ]);
  });

  it('shows the freed amount as a plain decrease when no B1 receives it', async () => {
    // precedence._with_b3_allocations without a receiver: no allocations, all freed unallocated.
    const b3 = withExtra(backendB3(), {
      allocations: undefined,
      allocated: '0.00',
      unallocated: '30.00',
    });
    await renderBudget(budgetWith([b3]));

    const article = card(B3_ACTION);
    expect(
      within(article).queryByRole('list', { name: 'Reallocations to recommended increases' }),
    ).toBeNull();
    expect(within(article).getByText('Plain decrease: $30.00')).toBeTruthy();
  });

  it('falls back to the freed amount when no split is recorded', async () => {
    const b3 = withExtra(backendB3(), {
      allocations: undefined,
      allocated: undefined,
      unallocated: undefined,
    });
    await renderBudget(budgetWith([b3]));
    expect(within(card(B3_ACTION)).getByText('Plain decrease: $30.00')).toBeTruthy();
  });

  it('never computes a remainder the backend did not record', async () => {
    const b3 = withExtra(backendB3(), { unallocated: undefined });
    await renderBudget(budgetWith([b3]));

    const article = card(B3_ACTION);
    expect(listLines(article, 'Reallocations to recommended increases')).toHaveLength(1);
    expect(within(article).queryByText(/^Plain decrease/u)).toBeNull();
  });

  it('shows no reallocation part without allocations, remainder or freed amount', async () => {
    const b3 = withExtra(backendB3(), {
      allocations: undefined,
      unallocated: undefined,
      freed: undefined,
    });
    await renderBudget(budgetWith([b3]));

    const article = card(B3_ACTION);
    expect(
      within(article).queryByRole('list', { name: 'Reallocations to recommended increases' }),
    ).toBeNull();
    expect(within(article).queryByText(/^Plain decrease/u)).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// B3 allocation acceptance case (AAP 0.11.2 test_budget_rules "B3 allocation")
// ---------------------------------------------------------------------------------------------

describe('reallocation from two donors into one B1 increase of 400', () => {
  /** The receiver: a B1 raising its budget by USD 400.00, all of it funded by reallocation. */
  function receiver(): Item {
    return budgetItem({
      id: 5001,
      rule_id: 'B1',
      action: 'Raise daily budget from USD 2,000.00 to USD 2,400.00',
      metrics: [
        usd('cost_per_outcome', 'Cost per conversion', '50.00'),
        usd('daily_budget', 'Latest effective daily budget', '2000.00'),
        usd('proposed_daily_budget', 'Proposed daily budget', '2400.00'),
        metric('outcomes', 'Window conversions', 80, 'conversions'),
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.97', 'ratio'),
      ],
      extra: {
        currency: 'USD',
        increase: '400.00',
        allocations: [
          { donor: { type: 'campaign', id: 5002 }, donor_name: 'Donor 300', amount: '300.00' },
          { donor: { type: 'campaign', id: 5003 }, donor_name: 'Donor 200', amount: '100.00' },
        ],
        funded_by_reallocation: '400.00',
        from_new_budget: '0.00',
        funding:
          'Raise by USD 400.00: USD 400.00 funded by reallocation from Donor 300, Donor 200, ' +
          'USD 0.00 from new budget',
      },
      decision_subject: { type: 'campaign', id: 5001 },
    });
  }

  /** A donor B3 freeing `freed` and moving `amount` of it to the receiver. */
  function donor(id: number, freed: string, amount: string, unallocated: string): Item {
    return budgetItem({
      id,
      rule_id: 'B3',
      action: `Lower daily budget of donor ${String(id)}`,
      metrics: [usd('daily_budget', 'Latest effective daily budget', '1000.00')],
      extra: {
        currency: 'USD',
        freed,
        allocated: amount,
        unallocated,
        allocations: [
          {
            receiver: { type: 'campaign', id: 5001 },
            receiver_name: 'Receiver — Lead gen',
            amount,
            text: `Move USD ${amount} to fund Receiver — Lead gen's recommended increase (B1)`,
          },
        ],
      },
      decision_subject: { type: 'campaign', id },
    });
  }

  it('funds 400 by reallocation: 300 and 100, with 100 a plain decrease', async () => {
    await renderBudget(
      budgetWith([
        receiver(),
        donor(5002, '300.00', '300.00', '0.00'),
        donor(5003, '200.00', '100.00', '100.00'),
      ]),
    );

    expect(
      within(card('Raise daily budget from USD 2,000.00 to USD 2,400.00')).getByText(
        'Raise by USD 400.00: USD 400.00 funded by reallocation from Donor 300, Donor 200, ' +
          'USD 0.00 from new budget',
      ),
    ).toBeTruthy();

    const first = card('Lower daily budget of donor 5002');
    expect(listLines(first, 'Reallocations to recommended increases')).toEqual([
      "Move USD 300.00 to fund Receiver — Lead gen's recommended increase (B1)",
    ]);

    const second = card('Lower daily budget of donor 5003');
    expect(listLines(second, 'Reallocations to recommended increases')).toEqual([
      "Move USD 100.00 to fund Receiver — Lead gen's recommended increase (B1)",
    ]);
    expect(within(second).getByText('Plain decrease: $100.00')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------------------------
// B2, B4 and the edge cases of each part
// ---------------------------------------------------------------------------------------------

describe('other budget cards', () => {
  it('shows a B2 card its gate, the stricter spending minimum and CRM QL', async () => {
    // metrics.high_utilization over 10 known-budget days: spending_share and
    // high_utilization_days both require ⌈0.7 × 10⌉ = 7.
    const b2 = budgetItem({
      id: 6001,
      rule_id: 'B2',
      action: 'Do not increase. Lower daily budget by 20% or fix efficiency first',
      metrics: [
        usd('spend', 'Window spend', '2790.00'),
        metric('outcomes', 'Window leads', 9, 'leads'),
        usd('cost_per_outcome', 'Cost per lead', '310.00'),
        usd('daily_budget', 'Latest effective daily budget', '100.00'),
        usd('proposed_daily_budget', 'Proposed daily budget', '80.00'),
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.93', 'ratio'),
        days('known_days', 'Known-budget days', 10),
        days('spending_days', 'Spending days', 10),
        days('high_utilization_days', 'High-utilization days', 9),
        days('idle_days', 'Known idle days', 0),
        metric('eligible_contacts', 'Eligible contacts (matured leads)', 21, 'contacts'),
        metric('ql', 'Qualified Leads (CRM)', 1, 'contacts'),
      ],
      baselines: [baseline('baseline_cost_per_outcome', 'Account cost per lead', '120.00')],
      extra: {
        currency: 'USD',
        gate: {
          known_budget_days: { observed: 10, required: 7 },
          spending_days: { observed: 10, required: 5 },
          spending_share: { observed: 10, required: 7 },
          high_utilization_days: { observed: 9, required: 7 },
        },
        proposed: '80.00',
      },
      decision_subject: { type: 'campaign', id: 6001 },
    });
    await renderBudget(budgetWith([b2]));

    const article = card('Do not increase. Lower daily budget by 20% or fix efficiency first');
    // Utilization beside efficiency (AAP 0.5.5): sibling cells of the one grid, utilization first.
    const utilizationCell = namedGroup(article, 'Utilization');
    const efficiencyCell = namedGroup(article, 'Efficiency');
    expect(utilizationCell.parentElement).toBe(efficiencyCell.parentElement);
    expect(utilizationCell.parentElement).toBe(namedGroup(article, 'Utilization and efficiency'));
    expect(follows(utilizationCell, efficiencyCell)).toBe(true);
    expect(groupFields(article, 'Utilization')).toEqual([
      'Window utilization (spend vs cap): 93%',
      'Days (observed / required): Spending days 10 / 7 · High-utilization days 9 / 7',
    ]);
    expect(groupFields(article, 'Efficiency')).toEqual([
      'Cost per lead: $310.00',
      'Account cost per lead: $120.00',
    ]);
    expect(tableOf(article, 'Budget days').rows).toEqual([
      ['Known-budget days', '10', '7'],
      ['Spending days', '10', '7'],
      ['High-utilization days', '9', '7'],
      ['Known idle days', '0', '—'],
    ]);
    expect(listLines(article, 'Budget details')).toEqual([
      'Window leads: 9',
      'Qualified Leads (CRM): 1',
      'Latest effective daily budget: $100.00',
      'Proposed daily budget: $80.00',
    ]);
    expect(within(article).queryByText(/^Raise by/u)).toBeNull();
    expect(within(article).queryByText(/^Plain decrease/u)).toBeNull();
  });

  it("shows a B4 card utilization n/a and its shared budget's members", async () => {
    const b4 = budgetItem({
      id: 6002,
      rule_id: 'B4',
      action: 'Adjust weekend schedule or bids: weekend cost/outcome $90.00 vs weekday $45.00',
      metrics: [
        metric('outcomes', 'Window conversions', 30, 'conversions'),
        usd('cost_per_outcome', 'Cost per conversion', '60.00'),
        metric('window_utilization', 'Window utilization (spend vs cap)', 'N/A', 'ratio'),
        days('known_days', 'Budget-active days', 0),
        usd('daily_budget', 'Latest effective daily budget', null),
      ],
      extra: {
        currency: 'USD',
        gate: {
          weekend_days: { observed: 8, required: 4 },
          weekdays: { observed: 20, required: 10 },
          outcomes: { observed: 30, required: 10 },
        },
        members: [
          { campaign_id: 7001, name: 'Brand — US', member_days: 1 },
          { id: 7002, name: 'Brand — CA', member_days: null },
          { campaign_id: 7003 },
          'not a member',
        ],
      },
    });
    await renderBudget(budgetWith([b4]));

    const article = card(
      'Adjust weekend schedule or bids: weekend cost/outcome $90.00 vs weekday $45.00',
    );
    expect(groupFields(article, 'Utilization')).toEqual(['Window utilization (spend vs cap): n/a']);
    // B4 is not gated on budget history, so no requirement is shown.
    expect(tableOf(article, 'Budget days').rows).toEqual([['Budget-active days', '0', '—']]);
    expect(listLines(article, 'Budget details')).toEqual([
      'Window conversions: 30',
      'Latest effective daily budget: unavailable',
    ]);
    expect(listLines(article, 'Shared budget members')).toEqual([
      'Brand — US · 1 member day',
      'Brand — CA · member days unavailable',
    ]);
    // No decision subject: the card offers "Mark done" (AAP 0.5.5).
    expect(within(article).getByRole('button', { name: 'Mark done' })).toBeTruthy();
    expect(within(article).queryByRole('button', { name: 'Record decision' })).toBeNull();
  });

  it('says the members are unavailable on a shared budget whose evidence lists none', async () => {
    const shared = budgetItem({
      id: 6003,
      rule_id: 'B2',
      action: 'Do not increase the shared budget',
      metrics: [usd('cost_per_outcome', 'Cost per conversion', '99.00')],
      extra: { currency: 'USD' },
      current: makeItemOverlay({ flags_now: [makeFlag('shared_budget')] }),
    });
    const subject = budgetItem({
      id: 6004,
      rule_id: 'B3',
      action: 'Lower the shared budget',
      metrics: [usd('cost_per_outcome', 'Cost per conversion', '20.00')],
      extra: { currency: 'USD' },
      decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
    });
    await renderBudget(budgetWith([shared, subject]));

    for (const action of ['Do not increase the shared budget', 'Lower the shared budget']) {
      const article = card(action);
      expect(within(article).getByText('Members unavailable')).toBeTruthy();
      expect(within(article).queryByRole('list', { name: 'Shared budget members' })).toBeNull();
    }
  });

  it('shows no member part on an own-budget card without members', async () => {
    const own = budgetItem({
      id: 6005,
      rule_id: 'B2',
      action: 'Do not increase',
      metrics: [usd('cost_per_outcome', 'Cost per conversion', '99.00')],
      extra: { currency: 'USD' },
    });
    await renderBudget(budgetWith([own]));

    const article = card('Do not increase');
    expect(within(article).queryByText('Shared budget members')).toBeNull();
    expect(within(article).queryByText('Members unavailable')).toBeNull();
  });
});

describe('utilization and efficiency edge cases', () => {
  it('renders "—" for a cost over known zero outcomes, else unavailable', async () => {
    const zero = budgetItem({
      id: 6101,
      rule_id: 'B2',
      action: 'Zero outcomes as a number',
      metrics: [
        metric('cost_per_outcome', 'Cost per conversion', null, 'currency', 'EUR'),
        metric('outcomes', 'Window conversions', 0, 'conversions'),
        metric('window_utilization', 'Window utilization (spend vs cap)', 'n/a', 'ratio'),
      ],
      baselines: [baseline('baseline_cost_per_outcome', 'Account cost per conversion', '75.00')],
      extra: { currency: 'EUR' },
    });
    const zeroText = budgetItem({
      id: 6102,
      rule_id: 'B2',
      action: 'Zero outcomes as a decimal string',
      metrics: [
        metric('cost_per_outcome', 'Cost per conversion', null, 'currency', 'USD'),
        metric('outcomes', 'Window conversions', '0.0', 'conversions'),
      ],
      extra: { currency: 'USD' },
    });
    const fractional = budgetItem({
      id: 6103,
      rule_id: 'B2',
      action: 'Fractional outcomes',
      metrics: [
        metric('cost_per_outcome', 'Cost per conversion', null, 'currency', 'USD'),
        metric('outcomes', 'Window conversions', '0.5', 'conversions'),
      ],
      extra: { currency: 'USD' },
    });
    const unknown = budgetItem({
      id: 6104,
      rule_id: 'B2',
      action: 'Unknown outcomes',
      metrics: [
        metric('cost_per_outcome', 'Cost per conversion', null, 'currency', 'USD'),
        metric('outcomes', 'Window conversions', null, 'conversions'),
      ],
      extra: { currency: 'USD' },
    });
    await renderBudget(budgetWith([zero, zeroText, fractional, unknown]));

    const zeroCard = card('Zero outcomes as a number');
    expect(groupFields(zeroCard, 'Utilization')).toEqual([
      'Window utilization (spend vs cap): n/a',
    ]);
    // The baseline reads in the cost's own currency.
    expect(groupFields(zeroCard, 'Efficiency')).toEqual([
      'Cost per conversion: —',
      'Account cost per conversion: €75.00',
    ]);
    expect(groupFields(card('Zero outcomes as a decimal string'), 'Efficiency')).toEqual([
      'Cost per conversion: —',
      'Account baseline cost per outcome: unavailable',
    ]);
    expect(groupFields(card('Fractional outcomes'), 'Efficiency')[0]).toBe(
      'Cost per conversion: unavailable',
    );
    expect(groupFields(card('Unknown outcomes'), 'Efficiency')[0]).toBe(
      'Cost per conversion: unavailable',
    );
    expect(listLines(card('Unknown outcomes'), 'Budget details')[0]).toBe(
      'Window conversions: unavailable',
    );
  });

  it('falls back to generic labels when a part of the pair is missing', async () => {
    const noUtilization = budgetItem({
      id: 6201,
      rule_id: 'B2',
      action: 'Cost only',
      metrics: [usd('cost_per_outcome', 'Cost per conversion', '12.00')],
      baselines: [baseline('baseline_cost_per_outcome', 'Account cost per conversion', '10.00')],
      extra: { currency: 'USD' },
    });
    const noCost = budgetItem({
      id: 6202,
      rule_id: 'B2',
      action: 'Utilization only',
      metrics: [metric('window_utilization', 'Window utilization (spend vs cap)', '0.5', 'ratio')],
      extra: { currency: 'GBP' },
    });
    await renderBudget(budgetWith([noUtilization, noCost]));

    expect(groupFields(card('Cost only'), 'Utilization')).toEqual([
      'Window utilization: unavailable',
    ]);
    expect(groupFields(card('Utilization only'), 'Efficiency')).toEqual([
      'Cost per outcome: unavailable',
      'Account baseline cost per outcome: unavailable',
    ]);
    // Outcomes and the daily budget are always listed, and say so when absent.
    expect(listLines(card('Utilization only'), 'Budget details')).toEqual([
      'Outcomes: unavailable',
      'Daily budget: unavailable',
    ]);
    // No day counts, so no Budget days table and no day summary.
    expect(
      within(card('Utilization only')).queryByRole('table', { name: 'Budget days' }),
    ).toBeNull();
  });

  it('leaves out the utilization and efficiency grid when the evidence has neither', async () => {
    const bare = budgetItem({
      id: 6301,
      rule_id: 'B4',
      action: 'Pattern without budget history',
      metrics: [metric('outcomes', 'Window leads', 12, 'leads')],
      extra: null,
    });
    await renderBudget(budgetWith([bare]));

    const article = card('Pattern without budget history');
    expect(within(article).queryByRole('group', { name: 'Utilization and efficiency' })).toBeNull();
    expect(within(article).queryByRole('table', { name: 'Budget days' })).toBeNull();
    expect(listLines(article, 'Budget details')).toEqual([
      'Window leads: 12',
      'Daily budget: unavailable',
    ]);
  });
});

// ---------------------------------------------------------------------------------------------
// Currencies: each card in its own account currency, never summed (AAP 0.1.1)
// ---------------------------------------------------------------------------------------------

describe('amounts in each account currency', () => {
  /** A B2 on a USD account. */
  function usdB2(): Item {
    return budgetItem({
      id: 6401,
      rule_id: 'B2',
      action: 'Do not increase the US campaign',
      metrics: [
        usd('spend', 'Window spend', '2790.00'),
        metric('outcomes', 'Window leads', 9, 'leads'),
        usd('cost_per_outcome', 'Cost per lead', '310.00'),
        usd('daily_budget', 'Latest effective daily budget', '100.00'),
        usd('proposed_daily_budget', 'Proposed daily budget', '80.00'),
      ],
      baselines: [baseline('baseline_cost_per_outcome', 'Account cost per lead', '120.00')],
      extra: { currency: 'USD', proposed: '80.00' },
      decision_subject: { type: 'campaign', id: 6401 },
    });
  }

  /** A B1 on a EUR account whose funding sentence is built from the API's amounts. */
  function eurB1(): Item {
    const eur = (key: string, label: string, value: string): Metric =>
      metric(key, label, value, 'currency', 'EUR');
    return budgetItem({
      id: 6402,
      rule_id: 'B1',
      action: 'Raise daily budget from EUR 200.00 to EUR 240.00',
      metrics: [
        eur('spend', 'Window spend', '4200.00'),
        metric('outcomes', 'Window conversions', 50, 'conversions'),
        eur('cost_per_outcome', 'Cost per conversion', '84.00'),
        eur('daily_budget', 'Latest effective daily budget', '200.00'),
        eur('proposed_daily_budget', 'Proposed daily budget', '240.00'),
      ],
      baselines: [baseline('baseline_cost_per_outcome', 'Account cost per conversion', '90.00')],
      extra: {
        currency: 'EUR',
        proposed: '240.00',
        increase: '40.00',
        funded_by_reallocation: '0.00',
        from_new_budget: '40.00',
        allocations: [],
      },
      decision_subject: { type: 'campaign', id: 6402 },
    });
  }

  it('formats each card in its own currency and shows no cross-card total', async () => {
    // The expected strings differ by currency, so the assertions below cannot pass vacuously.
    expect(formatMoney('100.00', 'EUR')).not.toBe(formatMoney('100.00', 'USD'));

    await renderBudget(budgetWith([usdB2(), eurB1()]));

    const usdCard = card('Do not increase the US campaign');
    expect(groupFields(usdCard, 'Efficiency')).toEqual([
      `Cost per lead: ${formatMoney('310.00', 'USD')}`,
      `Account cost per lead: ${formatMoney('120.00', 'USD')}`,
    ]);
    expect(listLines(usdCard, 'Budget details')).toEqual([
      'Window leads: 9',
      `Latest effective daily budget: ${formatMoney('100.00', 'USD')}`,
      `Proposed daily budget: ${formatMoney('80.00', 'USD')}`,
    ]);

    const eurCard = card('Raise daily budget from EUR 200.00 to EUR 240.00');
    expect(groupFields(eurCard, 'Efficiency')).toEqual([
      `Cost per conversion: ${formatMoney('84.00', 'EUR')}`,
      `Account cost per conversion: ${formatMoney('90.00', 'EUR')}`,
    ]);
    expect(listLines(eurCard, 'Budget details')).toEqual([
      'Window conversions: 50',
      `Latest effective daily budget: ${formatMoney('200.00', 'EUR')}`,
      `Proposed daily budget: ${formatMoney('240.00', 'EUR')}`,
    ]);
    expect(
      within(eurCard).getByText(
        `Raise by ${formatMoney('40.00', 'EUR')}: ${formatMoney('0.00', 'EUR')} funded by ` +
          `reallocation, ${formatMoney('40.00', 'EUR')} from new budget`,
      ),
    ).toBeTruthy();

    // Neither card borrows the other's currency.
    expect(usdCard.textContent).not.toContain('€');
    expect(eurCard.textContent).not.toContain('$');

    // No total line: nothing labelled a total, and neither the spend sum (2,790 + 4,200) nor
    // the daily-budget sum (100 + 200) appears in any currency.
    const section = document.getElementById('section-budget') as HTMLElement;
    expect(section.textContent).not.toMatch(/total/iu);
    expect(section.textContent).not.toMatch(/6,?990/u);
    expect(section.textContent).not.toMatch(/\b300\.00\b/u);
  });
});

// ---------------------------------------------------------------------------------------------
// Data-state flags
// ---------------------------------------------------------------------------------------------

describe('data-state flags on budget cards', () => {
  it('shows the simulated flag the run recorded and the stale flag the overlay adds', async () => {
    const b1 = backendB1();
    const stale: Item = {
      ...b1,
      current: makeItemOverlay({ flags_now: [makeFlag('stale'), makeFlag('incomplete')] }),
    };
    await renderBudget(budgetWith([stale]));

    const flags = listLines(card(B1_ACTION), 'Flags');
    expect(flags).toContain('Simulated data');
    expect(flags).toContain(makeFlag('stale').message);
    expect(flags).toContain(makeFlag('incomplete').message);
  });
});
