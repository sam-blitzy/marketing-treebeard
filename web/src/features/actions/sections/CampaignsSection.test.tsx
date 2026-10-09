/**
 * Component tests for `CampaignsSection`, the "Campaigns to pause or launch" section of
 * `/actions` (AAP 0.5.5; AAP 0.5.4.4 C0–C3; AAP 0.5.4.2 P1).
 *
 * They hold the section to its TSDoc contract on the default fixture page and on items built
 * here in the backend's shape:
 * - the groups Discrepancies, Pause, Scale and Launch ideas in API order, the discrepancy group
 *   first and anchored at `#group-campaigns-discrepancies`;
 * - C0 cards: "Platform shows {platform}, last decided {decided}" and, in place of feedback
 *   actions, exactly the resolutions `GET /discrepancies` lists for the live discrepancy, with
 *   its loading, failed, resolved-since-run and missing-id states; a historical C0 stays pinned
 *   with its resolver disabled;
 * - held cards collapsed at the end of their group, linked to the discrepancy group;
 * - the C1, C1b and C2 field sets ("Campaign evidence", and the "Pipeline evidence" table with
 *   MQL and QL in separate columns for C1b and C2 "Hold" only);
 * - the C3a segment gap and the C3b keyword-cluster gap;
 * - the empty discrepancy group kept, with the live count and a link to `/decisions`, while the
 *   overlay counts discrepancies detected after the run;
 * - no `role="alert"` anywhere in the section.
 *
 * The network is stubbed with `mockApi`, which fails a test on any request it does not expect,
 * so a section without a C0 card is also shown to send no request at all.
 */
import { apiError, json, mockApi } from '../../../test/mockApi';
import { renderWithProviders } from '../../../test/render';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Item, Metric, Overlay, Section } from '../../../api/types';
import {
  FIXTURE_DISCREPANCY_ID,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeDiscrepancy,
  makeEvidence,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
} from '../../../test/fixtures';
import {
  CAMPAIGN_EVIDENCE_LABEL,
  CLUSTER_GAP_LABEL,
  CLUSTER_TERMS_CAPTION,
  CampaignsSection,
  DISCREPANCY_LOAD_ERROR_PREFIX,
  DISCREPANCY_UNAVAILABLE_TEXT,
  LOADING_DISCREPANCY_LABEL,
  PIPELINE_EVIDENCE_CAPTION,
  RESOLVED_SINCE_RUN_TEXT,
  RESOLVE_ON_DECISIONS_LABEL,
  SEGMENT_GAP_LABEL,
} from './CampaignsSection';
import { DISCREPANCY_GROUP_ANCHOR } from './SectionFrame';

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

/** The default page's Campaigns section. */
function defaultCampaigns(): Section {
  const section = makeRecommendationsPage().sections.find((entry) => entry.key === 'campaigns');
  if (section === undefined) {
    throw new Error('the default fixture page has no campaigns section');
  }
  return section;
}

/** The default page's item `id`. */
function pageItem(id: Item['id']): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** A Campaigns section holding exactly these groups, in this order. */
function campaignsWith(groups: Section['groups']): Section {
  return makeSection('campaigns', { groups });
}

/** A Campaigns section with one group of the given key holding `items`. */
function oneGroup(key: string, title: string, items: Item[]): Section {
  return campaignsWith([makeGroup({ key, title, items })]);
}

/** Renders the section with the default run and the given overlay, inside the providers. */
async function renderCampaigns(section: Section, overlay: Overlay = makeOverlay()) {
  return renderWithProviders(
    <CampaignsSection section={section} run={makeRun()} overlay={overlay} />,
  );
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

/** The header texts and every body row's cell texts of the table with this caption. */
function tableOf(scope: HTMLElement, caption: string): { headers: string[]; rows: string[][] } {
  const table = within(scope).getByRole('table', { name: caption });
  const headers = within(table)
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent);
  // Body rows only: each row's header cell (when the table has one) and its data cells.
  const rows = within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent));
  return { headers, rows };
}

/** The C0 action of the default page. */
const C0_ACTION = 'Resolve: platform shows paused, last decided active';

/** The live list the C0 card of the default page reads. */
function openDiscrepancies() {
  return json(200, [makeDiscrepancy('status')]);
}

// ---------------------------------------------------------------------------------------------
// Section layout
// ---------------------------------------------------------------------------------------------

describe('CampaignsSection layout', () => {
  it('renders Discrepancies, Pause, Scale and Launch ideas in API order', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(defaultCampaigns());

    const section = screen.getByRole('region', { name: 'Campaigns to pause or launch' });
    expect(section.id).toBe('section-campaigns');
    expect(
      within(section)
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(['Discrepancies', 'Pause', 'Scale', 'Launch ideas']);

    const discrepancies = screen.getByRole('region', { name: 'Discrepancies' });
    expect(discrepancies.id).toBe(DISCREPANCY_GROUP_ANCHOR);
    expect(discrepancies.id).toBe('group-campaigns-discrepancies');
    expect(screen.getByRole('region', { name: 'Pause' }).id).toBe('group-campaigns-pause');
    expect(screen.getByRole('region', { name: 'Scale' }).id).toBe('group-campaigns-scale');
    expect(screen.getByRole('region', { name: 'Launch ideas' }).id).toBe('group-campaigns-launch');

    // Every card heads at level 4, below its group's <h3>, and carries its item anchor.
    expect(within(discrepancies).getByRole('heading', { level: 4, name: C0_ACTION })).toBeTruthy();
    expect(card(C0_ACTION).id).toBe(`item-${String(FIXTURE_ITEM_IDS.c0)}`);

    // The open-discrepancies banner is the page's only alert (AAP 0.5.5 Behaviour).
    expect(screen.queryByRole('alert')).toBeNull();
    await screen.findByRole('button', { name: 'Platform is correct' });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps the API rank order inside a group and collapses held cards at its end', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(defaultCampaigns());

    const pause = screen.getByRole('region', { name: 'Pause' });
    const toggle = within(pause).getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    const heldRegion = document.getElementById('group-campaigns-pause-held');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe('group-campaigns-pause-held');
    expect(heldRegion?.hidden).toBe(true);

    const visibleCard = card('Pause');
    expect(visibleCard.id).toBe(`item-${String(FIXTURE_ITEM_IDS.c1)}`);
    expect(heldRegion?.contains(visibleCard)).toBe(false);

    const held = heldRegion?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.heldPause)}`);
    expect(held).not.toBeNull();
    expect(held?.textContent).toContain('Display Remarketing — US');
    expect(held?.textContent).toContain('Resolve discrepancy first');
    const link = within(held as HTMLElement).getByRole('link', {
      name: 'View blocking discrepancy',
      hidden: true,
    });
    expect(link.getAttribute('href')).toBe(`#${DISCREPANCY_GROUP_ANCHOR}`);

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(heldRegion?.hidden).toBe(false);
    await screen.findByRole('button', { name: 'Platform is correct' });
  });

  it('shows an empty state when the section has no group with a card', async () => {
    const api = mockApi({});
    await renderCampaigns(makeSection('campaigns'), makeOverlay({ open_discrepancies: 0 }));

    expect(screen.getByText('No actions in this section')).toBeTruthy();
    expect(screen.getByText('No rule fired for this window')).toBeTruthy();
    expect(api.calls).toHaveLength(0);
  });

  it('renders an unknown rule as the plain card, so no item is dropped', async () => {
    const api = mockApi({});
    const future = makeItem({
      id: 3001,
      rule_id: 'C9',
      action: 'Review campaign naming',
      reason: 'A rule added to the backend later.',
      entity: { source: 'google', type: 'campaign', id: '21000399', name: 'Future — US' },
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [future]));

    const plain = card('Review campaign naming');
    expect(plain.textContent).toContain('A rule added to the backend later.');
    expect(within(plain).queryByRole('list', { name: CAMPAIGN_EVIDENCE_LABEL })).toBeNull();
    expect(within(plain).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
    expect(api.calls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------------------------
// C0 discrepancies
// ---------------------------------------------------------------------------------------------

describe('C0 discrepancy cards', () => {
  it('compares platform and decision, and offers live resolutions, not feedback', async () => {
    const api = mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(defaultCampaigns());

    const c0 = card(C0_ACTION);
    expect(c0.textContent).toContain('Platform shows paused, last decided active');
    // While the first read runs, the footer holds a polite spinner, not the controls.
    const loading = within(c0).getByText(LOADING_DISCREPANCY_LABEL);
    expect(loading.closest('[role="status"]')).not.toBeNull();
    expect(within(c0).queryByRole('button', { name: 'Platform is correct' })).toBeNull();

    await within(c0).findByRole('button', { name: 'Platform is correct' });
    expect(within(c0).getByRole('button', { name: 'Decision is correct' })).toBeTruthy();
    expect(within(c0).queryByText(LOADING_DISCREPANCY_LABEL)).toBeNull();
    // C0 is exempt from dismiss, snooze and done, and records no decision from its footer.
    for (const label of ['Dismiss', 'Snooze for 7 days', 'Mark done', 'Record decision']) {
      expect(within(c0).queryByRole('button', { name: label })).toBeNull();
    }

    // One read of the open and awaiting-platform discrepancies.
    const reads = api.calls.filter((call) => call.path === '/discrepancies');
    expect(reads).toHaveLength(1);
    expect(reads[0]?.method).toBe('GET');
    expect(reads[0]?.search.getAll('state')).toEqual(['open', 'awaiting_platform']);
  });

  it('offers exactly the resolutions the API lists, never derived ones', async () => {
    // The run froze a status discrepancy; the live list now reports it as untracked.
    mockApi({
      'GET /discrepancies': json(200, [
        makeDiscrepancy('untracked', { id: FIXTURE_DISCREPANCY_ID }),
      ]),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await within(c0).findByRole('button', { name: 'Adopt platform state' });
    expect(within(c0).getByRole('button', { name: 'Record intended state' })).toBeTruthy();
    expect(within(c0).queryByRole('button', { name: 'Platform is correct' })).toBeNull();
    expect(within(c0).queryByRole('button', { name: 'Decision is correct' })).toBeNull();
  });

  it('resolves the live discrepancy, then shows that it was resolved since the run', async () => {
    const api = mockApi({
      'GET /discrepancies': openDiscrepancies(),
      'POST /discrepancies/:id/resolve': (call) =>
        json(
          200,
          makeDiscrepancy('status', {
            id: Number(call.params.id),
            state: 'resolved',
            resolution: 'platform_correct',
            resolved_at: '2026-10-06T09:30:00Z',
          }),
        ),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    const platformCorrect = await within(c0).findByRole('button', { name: 'Platform is correct' });
    api.use({ 'GET /discrepancies': json(200, []) });
    fireEvent.click(platformCorrect);

    await within(c0).findByText(RESOLVED_SINCE_RUN_TEXT);
    const post = api.calls.find((call) => call.method === 'POST');
    expect(post?.path).toBe(`/discrepancies/${String(FIXTURE_DISCREPANCY_ID)}/resolve`);
    expect(post?.body).toEqual({ resolution: 'platform_correct' });
    expect(within(c0).queryByRole('button', { name: 'Decision is correct' })).toBeNull();
  });

  it('says it was resolved since the run when the live list lacks it', async () => {
    mockApi({
      'GET /discrepancies': json(200, [
        makeDiscrepancy('budget', { id: FIXTURE_DISCREPANCY_ID + 1 }),
      ]),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await within(c0).findByText(RESOLVED_SINCE_RUN_TEXT);
    expect(within(c0).queryByRole('button')).toBeNull();
    // The frozen comparison still states what the run saw.
    expect(c0.textContent).toContain('Platform shows paused, last decided active');
  });

  it("shows the API's message when the live list cannot be read", async () => {
    mockApi({
      'GET /discrepancies': apiError(503, 'database_unavailable', 'The database is unavailable.'),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await within(c0).findByText(`${DISCREPANCY_LOAD_ERROR_PREFIX}: The database is unavailable.`);
    expect(within(c0).queryByRole('button')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('falls back to a generic detail when the failed read carries a blank message', async () => {
    mockApi({ 'GET /discrepancies': apiError(500, 'internal_error', '   ') });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    await within(card(C0_ACTION)).findByText(
      `${DISCREPANCY_LOAD_ERROR_PREFIX}: the request failed`,
    );
  });

  it('says details are unavailable, reading nothing, without a discrepancy id', async () => {
    const api = mockApi({});
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    const orphan: Item = {
      ...c0,
      evidence: makeEvidence({ ...c0.evidence, extra: { kind: 'status', shape: 'own_budget' } }),
    };
    await renderCampaigns(oneGroup('discrepancies', 'Discrepancies', [orphan]));

    const article = card(C0_ACTION);
    expect(within(article).getByText(DISCREPANCY_UNAVAILABLE_TEXT)).toBeTruthy();
    expect(article.textContent).not.toContain('Platform shows');
    expect(within(article).queryByRole('button')).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it('keeps a historical C0 pinned in place with every resolution disabled', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    // The overlay holds every item computed from replaced data, C0 included (AAP 0.3.2).
    const historical: Item = {
      ...c0,
      current: makeItemOverlay({
        historical: true,
        held_now: true,
        held_reason_now: 'Computed from data that has since been replaced',
        data_status_now: 'live',
      }),
    };
    await renderCampaigns(oneGroup('discrepancies', 'Discrepancies', [historical]));

    const group = screen.getByRole('region', { name: 'Discrepancies' });
    // Pinned, never behind the held disclosure.
    expect(
      within(group).queryByRole('button', { name: /^Held until discrepancies are resolved/u }),
    ).toBeNull();
    const article = card(C0_ACTION);
    expect(group.contains(article)).toBe(true);
    expect(article.textContent).toContain('Computed from replaced data');
    expect(within(article).queryByRole('link', { name: 'View blocking discrepancy' })).toBeNull();

    const platformCorrect = await within(article).findByRole('button', {
      name: 'Platform is correct',
    });
    expect((platformCorrect as HTMLButtonElement).disabled).toBe(true);
    const decisionCorrect = within(article).getByRole('button', { name: 'Decision is correct' });
    expect((decisionCorrect as HTMLButtonElement).disabled).toBe(true);
  });

  it('pins C0 cards and puts other held items behind the disclosure', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    const held = pageItem(FIXTURE_ITEM_IDS.heldPause);
    const hidden = makeItem({
      id: 3002,
      rule_id: 'C1',
      action: 'Pause dismissed campaign',
      current: makeItemOverlay({ hidden_by: 'dismiss' }),
    });
    const visible = makeItem({
      id: 3003,
      rule_id: 'C1',
      action: 'Pause visible campaign',
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [
        held,
        pageItem(FIXTURE_ITEM_IDS.c0),
        hidden,
        visible,
      ]),
    );

    const group = screen.getByRole('region', { name: 'Discrepancies' });
    const toggle = within(group).getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    expect(toggle.getAttribute('aria-controls')).toBe(`${DISCREPANCY_GROUP_ANCHOR}-held`);
    const heldRegion = document.getElementById(`${DISCREPANCY_GROUP_ANCHOR}-held`);
    expect(heldRegion?.hidden).toBe(true);
    expect(heldRegion?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.heldPause)}`)).not.toBeNull();

    // The C0 and the visible card are pinned in API order, before the disclosure.
    const pinned = [...group.querySelectorAll('article')].filter(
      (article) => heldRegion?.contains(article) !== true,
    );
    expect(pinned.map((article) => article.id)).toEqual([
      `item-${String(FIXTURE_ITEM_IDS.c0)}`,
      'item-3003',
    ]);
    // The hidden card is skipped here and counted by the frame.
    expect(
      screen.queryByRole('article', { name: 'Pause dismissed campaign', hidden: true }),
    ).toBeNull();
    expect(
      listLines(screen.getByRole('region', { name: 'Campaigns to pause or launch' }), 'Not shown'),
    ).toEqual(['1 hidden since this run']);
    await within(card(C0_ACTION)).findByRole('button', { name: 'Platform is correct' });
  });
});

// ---------------------------------------------------------------------------------------------
// Empty discrepancy group
// ---------------------------------------------------------------------------------------------

describe('the empty discrepancy group', () => {
  /** A section whose discrepancy group is empty in the run, beside one pause card. */
  function lateSection(): Section {
    return campaignsWith([
      makeGroup({ key: 'discrepancies', title: 'Discrepancies', items: [] }),
      makeGroup({ key: 'pause', title: 'Pause', items: [pageItem(FIXTURE_ITEM_IDS.c1)] }),
      makeGroup({ key: 'launch', title: 'Launch ideas', items: [] }),
    ]);
  }

  it('stays anchored with the live count and a /decisions link while open', async () => {
    const api = mockApi({});
    await renderCampaigns(lateSection(), makeOverlay({ open_discrepancies: 3 }));

    const group = screen.getByRole('region', { name: 'Discrepancies' });
    expect(group.id).toBe(DISCREPANCY_GROUP_ANCHOR);
    expect(group.textContent).toContain(
      '3 open discrepancies detected since this run was computed',
    );
    const link = within(group).getByRole('link', { name: RESOLVE_ON_DECISIONS_LABEL });
    expect(link.getAttribute('href')).toBe('/decisions');
    // It is still first, and every other empty group is left out.
    expect(
      screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(['Discrepancies', 'Pause']);
    // The notice reads no list.
    expect(api.calls).toHaveLength(0);
  });

  it('uses the singular for one open discrepancy', async () => {
    mockApi({});
    await renderCampaigns(lateSection(), makeOverlay({ open_discrepancies: 1 }));
    expect(screen.getByRole('region', { name: 'Discrepancies' }).textContent).toContain(
      '1 open discrepancy detected since this run was computed',
    );
  });

  it.each([
    ['zero', 0],
    ['a fraction', 1.5],
    ['a negative count', -2],
    ['NaN', Number.NaN],
  ])('is left out when the live count is %s', async (_label, count) => {
    mockApi({});
    await renderCampaigns(lateSection(), makeOverlay({ open_discrepancies: count }));
    expect(screen.queryByRole('region', { name: 'Discrepancies' })).toBeNull();
    expect(document.getElementById(DISCREPANCY_GROUP_ANCHOR)).toBeNull();
    expect(screen.getByRole('region', { name: 'Pause' })).toBeTruthy();
  });

  it('keeps the anchor in an otherwise empty section beside its empty state', async () => {
    mockApi({});
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', []),
      makeOverlay({ open_discrepancies: 2 }),
    );
    expect(document.getElementById(DISCREPANCY_GROUP_ANCHOR)?.textContent).toContain(
      '2 open discrepancies detected since this run was computed',
    );
    expect(screen.getByText('No actions in this section')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------------------------
// Pause, scale and hold field sets (C1, C1b, C2)
// ---------------------------------------------------------------------------------------------

describe('pause and scale cards', () => {
  it('shows a C1 card spend, outcomes and expected outcomes, no pipeline', async () => {
    mockApi({});
    await renderCampaigns(oneGroup('pause', 'Pause', [pageItem(FIXTURE_ITEM_IDS.c1)]));

    const c1 = card('Pause');
    expect(listLines(c1, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: $314.50',
      'Window conversions: 0',
      'Expected conversions at the account baseline: 3.4',
    ]);
    expect(within(c1).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
  });

  it('adds the CRM Qualified Leads to a linked C1 card', async () => {
    mockApi({});
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    const linked: Item = {
      ...c1,
      evidence: makeEvidence({
        ...c1.evidence,
        metrics: [...c1.evidence.metrics, metric('ql', 'Qualified Leads (CRM)', 2, 'contacts')],
      }),
    };
    await renderCampaigns(oneGroup('pause', 'Pause', [linked]));

    expect(listLines(card('Pause'), CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: $314.50',
      'Window conversions: 0',
      'Expected conversions at the account baseline: 3.4',
      'Qualified Leads (CRM): 2',
    ]);
  });

  it('shows a C2 "Hold" the pipeline counts, MQL and QL in separate columns', async () => {
    mockApi({});
    await renderCampaigns(oneGroup('scale', 'Scale', [pageItem(FIXTURE_ITEM_IDS.c2Hold)]));

    const hold = card('Hold: platform outcomes are not reaching Qualified Lead');
    expect(listLines(hold, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: $1,500.00',
      'Window leads: 48',
      'Qualified Leads: 0',
    ]);
    const pipeline = tableOf(hold, PIPELINE_EVIDENCE_CAPTION);
    expect(pipeline.headers).toEqual([
      'Matured leads',
      'Matched matured leads',
      'Unresolved matured leads',
      'Eligible contacts',
      'MQL (First meeting booked)',
      'Qualified Leads',
      'Meetings',
    ]);
    expect(pipeline.rows).toEqual([['30', '28', '2', '26', '4', '0', '0']]);
    // No header or cell combines MQL and QL.
    expect(pipeline.headers.some((header) => /MQL.*Qualified|Qualified.*MQL/u.test(header))).toBe(
      false,
    );
  });

  it('shows a C2 "Scale" no pipeline table', async () => {
    mockApi({});
    const hold = pageItem(FIXTURE_ITEM_IDS.c2Hold);
    const scale: Item = {
      ...hold,
      id: 3004,
      action: 'Scale',
      evidence: makeEvidence({
        ...hold.evidence,
        extra: { ...hold.evidence.extra, outcome: 'Scale', veto: { fired: false } },
      }),
    };
    await renderCampaigns(oneGroup('scale', 'Scale', [scale]));

    const article = card('Scale');
    expect(listLines(article, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: $1,500.00',
      'Window leads: 48',
      'Qualified Leads: 0',
    ]);
    expect(within(article).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
  });

  it('shows a C1b card its spend and the pipeline counts side by side', async () => {
    mockApi({});
    const c1b = makeItem({
      id: 3005,
      rule_id: 'C1b',
      action: 'Pause or rework targeting: leads are not becoming Qualified Leads',
      reason: '22 eligible contacts from matured leads reached 0 Qualified Leads and 0 meetings.',
      entity: {
        source: 'linkedin',
        type: 'campaign',
        id: '700000602',
        name: 'Lead Gen — Ops EMEA',
      },
      evidence: makeEvidence({
        metrics: [
          metric('spend', 'Window spend', '2240.00', 'currency', 'EUR'),
          metric('leads', 'LinkedIn leads (platform)', 41, 'leads'),
          metric(
            'matured_leads',
            'Matured leads (at least 14 days before window end)',
            25,
            'leads',
          ),
          metric('matched_leads', 'Matched matured leads', 24, 'leads'),
          metric('unresolved', 'Unresolved matured leads', 1, 'leads'),
          metric('eligible_contacts', 'Eligible contacts', 22, 'contacts'),
          metric('mql', 'MQL (First meeting booked)', 3, 'contacts'),
          metric('ql', 'Qualified Leads', 0, 'contacts'),
          metric('meetings', 'Meetings', 0, 'meetings'),
        ],
        extra: { proposed_status: 'paused' },
      }),
      decision_subject: { type: 'campaign', id: 12 },
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [c1b]));

    const article = card('Pause or rework targeting: leads are not becoming Qualified Leads');
    // C1b records no outcomes or expected outcomes, so only spend and QL are listed.
    expect(listLines(article, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: €2,240.00',
      'Qualified Leads: 0',
    ]);
    const pipeline = tableOf(article, PIPELINE_EVIDENCE_CAPTION);
    expect(pipeline.headers).toEqual([
      'Matured leads',
      'Matched matured leads',
      'Unresolved matured leads',
      'Eligible contacts',
      'MQL (First meeting booked)',
      'Qualified Leads',
      'Meetings',
    ]);
    expect(pipeline.rows).toEqual([['25', '24', '1', '22', '3', '0', '0']]);
  });

  it('keeps the matured label without a unit and drops missing columns', async () => {
    mockApi({});
    const c1b = makeItem({
      id: 3006,
      rule_id: 'C1b',
      action: 'Pause or rework targeting',
      evidence: makeEvidence({
        metrics: [
          metric('matured_leads', 'Matured contacts (14 days)', 20, null),
          metric('mql', 'MQL (First meeting booked)', null, 'contacts'),
        ],
      }),
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [c1b]));

    const article = card('Pause or rework targeting');
    const pipeline = tableOf(article, PIPELINE_EVIDENCE_CAPTION);
    expect(pipeline.headers).toEqual(['Matured contacts (14 days)', 'MQL (First meeting booked)']);
    // An unavailable count reads "unavailable", never 0.
    expect(pipeline.rows).toEqual([['20', 'unavailable']]);
    // No metric of the campaign list, so the list is left out.
    expect(within(article).queryByRole('list', { name: CAMPAIGN_EVIDENCE_LABEL })).toBeNull();
  });

  it('leaves the pipeline table out when the evidence carries none of its metrics', async () => {
    mockApi({});
    const c1b = makeItem({
      id: 3007,
      rule_id: 'C1b',
      action: 'Pause or rework targeting',
      evidence: makeEvidence({
        metrics: [metric('spend', 'Window spend', '10.00', 'currency', 'USD')],
      }),
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [c1b]));

    const article = card('Pause or rework targeting');
    expect(within(article).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
    expect(listLines(article, CAMPAIGN_EVIDENCE_LABEL)).toEqual(['Window spend: $10.00']);
  });
});

// ---------------------------------------------------------------------------------------------
// Launch ideas (C3a, C3b)
// ---------------------------------------------------------------------------------------------

describe('launch idea cards', () => {
  it('shows a C3b card its head tokens and member terms', async () => {
    mockApi({});
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [pageItem(FIXTURE_ITEM_IDS.c3b)]));

    const c3b = card("Create ad group or campaign for 'data warehouse'");
    expect(listLines(c3b, CLUSTER_GAP_LABEL)).toEqual(['Head tokens: data, warehouse']);
    const terms = tableOf(c3b, CLUSTER_TERMS_CAPTION);
    expect(terms.headers).toEqual(['Term', 'Spend', 'Conversions']);
    // Conversions stay fractional under data-driven attribution.
    expect(terms.rows).toEqual([
      ['cloud data warehouse', '$132.00', '3'],
      ['data warehouse tools', '$96.00', '2.5'],
      ['data warehouse pricing', '$59.00', '1.5'],
    ]);
  });

  it('shows a cluster without head tokens only its terms', async () => {
    mockApi({});
    const c3b = makeItem({
      id: 3008,
      rule_id: 'C3b',
      action: "Create ad group or campaign for 'erp'",
      evidence: makeEvidence({
        metrics: [metric('spend', 'Cluster spend', '40.00', 'currency', 'USD')],
        extra: {
          head_tokens: [],
          members: [
            { term: 'erp suite', spend: null, conversions: '2', clicks: 9 },
            { term: 'erp pricing', spend: '40.00', conversions: null, clicks: 7 },
            { spend: '1.00', conversions: '1' },
          ],
        },
      }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [c3b]));

    const article = card("Create ad group or campaign for 'erp'");
    expect(within(article).queryByRole('list', { name: CLUSTER_GAP_LABEL })).toBeNull();
    // A member without a term is not a cluster term.
    expect(tableOf(article, CLUSTER_TERMS_CAPTION).rows).toEqual([
      ['erp suite', 'unavailable', '2'],
      ['erp pricing', '$40.00', 'unavailable'],
    ]);
  });

  it('shows a cluster with head tokens but no member term only its head tokens', async () => {
    mockApi({});
    const c3b = makeItem({
      id: 3009,
      rule_id: 'C3b',
      action: "Create ad group or campaign for 'crm migration'",
      evidence: makeEvidence({ extra: { head_tokens: ['crm', 'migration'], members: [] } }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [c3b]));

    const article = card("Create ad group or campaign for 'crm migration'");
    expect(listLines(article, CLUSTER_GAP_LABEL)).toEqual(['Head tokens: crm, migration']);
    expect(within(article).queryByRole('table', { name: CLUSTER_TERMS_CAPTION })).toBeNull();
  });

  it('shows a C3a card the segment, its leads, CPL and impression share', async () => {
    mockApi({});
    const action =
      'Launch a campaign targeting job function: Finance with ' +
      "Lead Gen — Pipeline Report EMEA's offer";
    const c3a = makeItem({
      id: 3010,
      rule_id: 'C3a',
      action,
      entity: {
        source: 'linkedin',
        type: 'campaign',
        id: '700000501',
        name: 'Lead Gen — Pipeline Report EMEA',
      },
      evidence: makeEvidence({
        metrics: [
          metric('segment_leads', 'Segment leads', 6, 'leads'),
          metric('segment_spend', 'Segment spend', '171.00', 'currency', 'EUR'),
          metric('segment_cpl', 'Segment cost per lead', '28.50', 'currency', 'EUR'),
          metric('campaign_cpl', 'Campaign cost per lead', '41.20', 'currency', 'EUR'),
          metric('impression_share', 'Segment share of campaign impressions', '0.12', 'ratio'),
        ],
        extra: { facet: 'JOB_FUNCTION', facet_label: 'job function', value: 'Finance' },
      }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [c3a]));

    const article = card(action);
    expect(listLines(article, SEGMENT_GAP_LABEL)).toEqual([
      'Segment: job function: Finance',
      'Segment leads: 6',
      'Segment cost per lead: €28.50',
      'Segment share of campaign impressions: 12%',
    ]);
    expect(within(article).queryByRole('table', { name: CLUSTER_TERMS_CAPTION })).toBeNull();
  });

  it('shows a launch idea without gap evidence as its plain card', async () => {
    mockApi({});
    const bare = makeItem({
      id: 3011,
      rule_id: 'C3a',
      action: 'Launch a campaign',
      evidence: makeEvidence({ extra: null }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [bare]));

    const article = card('Launch a campaign');
    expect(within(article).queryByRole('list', { name: SEGMENT_GAP_LABEL })).toBeNull();
    expect(within(article).queryByRole('list', { name: CLUSTER_GAP_LABEL })).toBeNull();
    expect(within(article).queryByRole('table', { name: CLUSTER_TERMS_CAPTION })).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Flags and data state
// ---------------------------------------------------------------------------------------------

describe('data-state flags on campaign cards', () => {
  it('shows the simulated, incomplete and unverified flags the run and overlay carry', async () => {
    mockApi({});
    await renderCampaigns(
      campaignsWith([
        makeGroup({ key: 'pause', title: 'Pause', items: [pageItem(FIXTURE_ITEM_IDS.c1)] }),
        makeGroup({
          key: 'launch',
          title: 'Launch ideas',
          items: [pageItem(FIXTURE_ITEM_IDS.c3b)],
        }),
      ]),
    );

    const c1Flags = listLines(card('Pause'), 'Flags');
    expect(c1Flags).toContain('Simulated data (Google)');
    expect(c1Flags).toContain('Unverified while 1 discrepancies are open');
    const c3bFlags = listLines(card("Create ad group or campaign for 'data warehouse'"), 'Flags');
    expect(c3bFlags).toContain(
      'Incomplete: google.search_terms (result limit reached at finest partition)',
    );
    expect(c3bFlags).toContain('Simulated data (Google)');
    await waitFor(() => {
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });
});
