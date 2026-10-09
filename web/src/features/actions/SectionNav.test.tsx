/**
 * Component tests for `SectionNav`, the `/actions` section navigation (AAP 0.5.5: "anchor links
 * with item counts for the five sections"; AAP 0.4.2: `SideNav` with anchor items and counts).
 *
 * They hold the component to its TSDoc contract:
 * - one `navigation` landmark named "Sections", with one in-page link per section in
 *   `SECTION_ORDER` (Keywords to adjust, Campaigns to pause or launch, Budget changes, Copy /
 *   creative changes, White papers), each pointing at `#section-{key}`, the region
 *   `SectionFrame` anchors;
 * - each link's text is the API title followed by the count of cards the section shows now:
 *   held items count, items hidden by feedback since the run do not, and a 0 is shown;
 * - the order is the client's own: shuffled sections are sorted, an unknown key follows the
 *   known ones in API order, a repeated key links once (its first section), and a key the API
 *   left out gets no link; a blank title falls back to the key.
 *
 * The component holds no state and fetches nothing, so it renders without providers.
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SECTION_ORDER } from '../../api/types';
import type { Item, Section, SectionKey } from '../../api/types';
import {
  FIXTURE_SECTION_TITLES,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeRecommendationsPage,
  makeSection,
} from '../../test/fixtures';
import { SectionNav, visibleItemCount } from './SectionNav';
import { sectionAnchorId } from './sections/SectionFrame';

/** The links of the "Sections" landmark, in document order. */
function navLinks(): HTMLElement[] {
  const nav = screen.getByRole('navigation', { name: 'Sections' });
  return within(nav).getAllByRole('link');
}

/** Each link as `[text, href]`, in document order. */
function linkSummary(): [string | null, string | null][] {
  return navLinks().map((link) => [link.textContent, link.getAttribute('href')]);
}

/** A K1 item with the given id and read-time overlay. */
function itemWith(id: number, overrides: Partial<Item> = {}): Item {
  return makeItem({ id, ...overrides });
}

/** A section of `key` whose single group holds `items`. */
function sectionWithItems(key: SectionKey, items: Item[]): Section {
  return makeSection(key, { groups: [makeGroup({ key: 'g', title: 'Group', items })] });
}

describe('SectionNav', () => {
  it('links the five sections of the default page in the user order, with their counts', () => {
    const page = makeRecommendationsPage();

    render(<SectionNav sections={page.sections} />);

    // Default page: K1, K2, K3; C0, C1, the held pause, C2 Hold, C3b (the held card counts);
    // B1, B3; CR3 and the held historical CR1 pair; three white-paper rows.
    expect(linkSummary()).toEqual([
      ['Keywords to adjust 3', '#section-keywords'],
      ['Campaigns to pause or launch 5', '#section-campaigns'],
      ['Budget changes 2', '#section-budget'],
      ['Copy / creative changes 2', '#section-creative'],
      ['White papers 3', '#section-white_papers'],
    ]);
    // Every link targets the anchor SectionFrame gives that section's region.
    expect(navLinks().map((link) => link.getAttribute('href'))).toEqual(
      SECTION_ORDER.map((key) => `#${sectionAnchorId(key)}`),
    );
    // The accessible name keeps the label and the count apart.
    expect(screen.getByRole('link', { name: 'Keywords to adjust 3' })).toBe(navLinks()[0]);
  });

  it('counts held items but not items hidden by feedback since the run', () => {
    const keywords = sectionWithItems('keywords', [
      itemWith(1),
      itemWith(2, { held: true, held_reason: 'Resolve discrepancy first' }),
      itemWith(3, { current: makeItemOverlay({ held_now: true }) }),
      itemWith(4, { current: makeItemOverlay({ hidden_by: 'dismiss' }) }),
      itemWith(5, { current: makeItemOverlay({ hidden_by: 'snooze' }) }),
      itemWith(6, { held: true, current: makeItemOverlay({ hidden_by: 'done' }) }),
    ]);

    render(<SectionNav sections={[keywords]} />);

    expect(visibleItemCount(keywords)).toBe(3);
    expect(linkSummary()).toEqual([['Keywords to adjust 3', '#section-keywords']]);
  });

  it('shows a count of 0 rather than omitting it, for no groups and for only hidden items', () => {
    const empty = makeSection('budget');
    const allHidden = sectionWithItems('creative', [
      itemWith(7, { current: makeItemOverlay({ hidden_by: 'snooze' }) }),
    ]);

    render(<SectionNav sections={[empty, allHidden]} />);

    expect(visibleItemCount(empty)).toBe(0);
    expect(visibleItemCount(allHidden)).toBe(0);
    expect(linkSummary()).toEqual([
      ['Budget changes 0', '#section-budget'],
      ['Copy / creative changes 0', '#section-creative'],
    ]);
    const count = navLinks()[0]?.querySelector('.ds-sidenav__count');
    expect(count?.textContent).toBe('0');
  });

  it('sums the visible items of every group of a section', () => {
    const campaigns = makeSection('campaigns', {
      groups: [
        makeGroup({ key: 'pause', title: 'Pause', items: [itemWith(11), itemWith(12)] }),
        makeGroup({ key: 'scale', title: 'Scale', items: [] }),
        makeGroup({
          key: 'launch',
          title: 'Launch ideas',
          items: [itemWith(13), itemWith(14, { current: makeItemOverlay({ hidden_by: 'done' }) })],
        }),
      ],
    });

    render(<SectionNav sections={[campaigns]} />);

    expect(visibleItemCount(campaigns)).toBe(3);
    expect(linkSummary()).toEqual([['Campaigns to pause or launch 3', '#section-campaigns']]);
  });

  it('orders shuffled sections by SECTION_ORDER and gives no link to a section left out', () => {
    const shuffled = [
      makeSection('white_papers'),
      makeSection('keywords'),
      makeSection('creative'),
      makeSection('campaigns'),
    ];
    const before = shuffled.map((section) => section.key);

    render(<SectionNav sections={shuffled} />);

    expect(navLinks().map((link) => link.getAttribute('href'))).toEqual([
      '#section-keywords',
      '#section-campaigns',
      '#section-creative',
      '#section-white_papers',
    ]);
    expect(screen.queryByRole('link', { name: /Budget changes/u })).toBeNull();
    // The input is never mutated.
    expect(shuffled.map((section) => section.key)).toEqual(before);
  });

  it('links unknown keys after the known ones, in API order', () => {
    // A key a later backend might add; the client does not know it.
    const later = { ...makeSection('keywords'), key: 'later' as SectionKey, title: 'Later' };
    const future = { ...makeSection('keywords'), key: 'future' as SectionKey, title: 'Future' };

    render(
      <SectionNav sections={[later, makeSection('budget'), future, makeSection('keywords')]} />,
    );

    expect(linkSummary()).toEqual([
      ['Keywords to adjust 0', '#section-keywords'],
      ['Budget changes 0', '#section-budget'],
      ['Later 0', '#section-later'],
      ['Future 0', '#section-future'],
    ]);
  });

  it('links a repeated key once, to its first section', () => {
    const first = sectionWithItems('budget', [itemWith(21), itemWith(22)]);
    const repeat = { ...sectionWithItems('budget', [itemWith(23)]), title: 'Budget again' };

    render(<SectionNav sections={[first, repeat]} />);

    expect(linkSummary()).toEqual([['Budget changes 2', '#section-budget']]);
  });

  it('falls back to the section key when the title is blank', () => {
    const untitled = makeSection('creative', { title: '   ' });
    const padded = makeSection('keywords', { title: `  ${FIXTURE_SECTION_TITLES.keywords}  ` });

    render(<SectionNav sections={[untitled, padded]} />);

    expect(linkSummary()).toEqual([
      ['Keywords to adjust 0', '#section-keywords'],
      ['creative 0', '#section-creative'],
    ]);
  });
});
