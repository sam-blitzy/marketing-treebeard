/**
 * Component tests for `SectionNav`, the `/actions` section navigation (AAP 0.5.5: "anchor links
 * with item counts for the five sections"; AAP 0.4.2: `SideNav` with anchor items and counts).
 *
 * They hold the component to its TSDoc contract:
 * - one `navigation` landmark named "Sections", with one in-page link per section in
 *   `SECTION_ORDER` (Keywords to adjust, Campaigns to pause or launch, Budget changes,
 *   Copy/creative changes, White papers), each pointing at `#section-{key}`, the region
 *   `SectionFrame` anchors;
 * - each link's text is the API title followed by the count of cards the section shows now:
 *   held items count, items hidden by feedback since the run do not, and a 0 is shown;
 * - the order is the client's own: shuffled sections are sorted, an unknown key follows the
 *   known ones in API order, a repeated key links once (its first section), and a key the API
 *   left out gets no link; a blank title falls back to the key.
 *
 * The component holds no state and fetches nothing, but it renders through
 * `renderWithProviders`, inside the query client and router every `/actions` component sees,
 * so the plain in-page `<a href>` links are checked in the same tree the page mounts them in.
 */
import { screen, within } from '@testing-library/react';
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
import { renderWithProviders } from '../../test/render';
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
  it('links the five sections of the default page in the user order, with their counts', async () => {
    const page = makeRecommendationsPage();

    await renderWithProviders(<SectionNav sections={page.sections} />);

    // Default page: K1, K2, K3; C0, C1, the held pause, C2 Hold, C3b (the held card counts);
    // B1, B3; CR3 and the held historical CR1 pair; three white-paper rows.
    expect(linkSummary()).toEqual([
      ['Keywords to adjust 3', '#section-keywords'],
      ['Campaigns to pause or launch 5', '#section-campaigns'],
      ['Budget changes 2', '#section-budget'],
      ['Copy/creative changes 2', '#section-creative'],
      ['White papers 3', '#section-white_papers'],
    ]);
    // Every link targets the anchor SectionFrame gives that section's region.
    expect(navLinks().map((link) => link.getAttribute('href'))).toEqual(
      SECTION_ORDER.map((key) => `#${sectionAnchorId(key)}`),
    );
    // The accessible name keeps the label and the count apart.
    expect(screen.getByRole('link', { name: 'Keywords to adjust 3' })).toBe(navLinks()[0]);
    // Each link's text includes the title its fixture section carries, in SECTION_ORDER.
    const links = navLinks();
    SECTION_ORDER.forEach((key, index) => {
      expect(links[index]?.textContent).toContain(FIXTURE_SECTION_TITLES[key]);
    });
    expect(links.map((link) => link.querySelector('.ds-sidenav__label')?.textContent)).toEqual(
      page.sections.map((section) => section.title),
    );
  });

  it('renders all five sections passed out of order in SECTION_ORDER', async () => {
    const page = makeRecommendationsPage();
    // Reversed, then the middle swapped: no section sits at its SECTION_ORDER position.
    const reversed = page.sections.toReversed();
    const shuffled = [reversed[1], reversed[0], reversed[3], reversed[4], reversed[2]].filter(
      (section): section is Section => section !== undefined,
    );
    expect(shuffled).toHaveLength(SECTION_ORDER.length);
    expect(shuffled.map((section) => section.key)).not.toEqual([...SECTION_ORDER]);

    await renderWithProviders(<SectionNav sections={shuffled} />);

    // The default page holds its sections in SECTION_ORDER, so it is the expected order.
    expect(page.sections.map((section) => section.key)).toEqual([...SECTION_ORDER]);
    expect(linkSummary()).toEqual(
      page.sections.map((section) => [
        `${section.title} ${visibleItemCount(section)}`,
        `#section-${section.key}`,
      ]),
    );
  });

  it('counts 2 of 3 items when one is dismissed since the run and one is held now', async () => {
    const keywords = sectionWithItems('keywords', [
      itemWith(31),
      itemWith(32, { current: makeItemOverlay({ hidden_by: 'dismiss' }) }),
      itemWith(33, { current: makeItemOverlay({ held_now: true }) }),
    ]);

    await renderWithProviders(<SectionNav sections={[keywords]} />);

    expect(visibleItemCount(keywords)).toBe(2);
    const link = screen.getByRole('link', { name: 'Keywords to adjust 2' });
    expect(link.getAttribute('href')).toBe('#section-keywords');
    expect(link.querySelector('.ds-sidenav__count')?.textContent).toBe('2');
  });

  it('counts held items but not items hidden by feedback since the run', async () => {
    const keywords = sectionWithItems('keywords', [
      itemWith(1),
      itemWith(2, { held: true, held_reason: 'Resolve discrepancy first' }),
      itemWith(3, { current: makeItemOverlay({ held_now: true }) }),
      itemWith(4, { current: makeItemOverlay({ hidden_by: 'dismiss' }) }),
      itemWith(5, { current: makeItemOverlay({ hidden_by: 'snooze' }) }),
      itemWith(6, { held: true, current: makeItemOverlay({ hidden_by: 'done' }) }),
    ]);

    await renderWithProviders(<SectionNav sections={[keywords]} />);

    expect(visibleItemCount(keywords)).toBe(3);
    expect(linkSummary()).toEqual([['Keywords to adjust 3', '#section-keywords']]);
  });

  it('shows a count of 0 rather than omitting it, for no groups and for only hidden items', async () => {
    const empty = makeSection('budget');
    const allHidden = sectionWithItems('creative', [
      itemWith(7, { current: makeItemOverlay({ hidden_by: 'snooze' }) }),
    ]);

    await renderWithProviders(<SectionNav sections={[empty, allHidden]} />);

    expect(visibleItemCount(empty)).toBe(0);
    expect(visibleItemCount(allHidden)).toBe(0);
    expect(linkSummary()).toEqual([
      ['Budget changes 0', '#section-budget'],
      ['Copy/creative changes 0', '#section-creative'],
    ]);
    const count = navLinks()[0]?.querySelector('.ds-sidenav__count');
    expect(count?.textContent).toBe('0');
  });

  it('sums the visible items of every group of a section', async () => {
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

    await renderWithProviders(<SectionNav sections={[campaigns]} />);

    expect(visibleItemCount(campaigns)).toBe(3);
    expect(linkSummary()).toEqual([['Campaigns to pause or launch 3', '#section-campaigns']]);
  });

  it('orders shuffled sections by SECTION_ORDER and gives no link to a section left out', async () => {
    const shuffled = [
      makeSection('white_papers'),
      makeSection('keywords'),
      makeSection('creative'),
      makeSection('campaigns'),
    ];
    const before = shuffled.map((section) => section.key);

    await renderWithProviders(<SectionNav sections={shuffled} />);

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

  it('links unknown keys after the known ones, in API order', async () => {
    // A key a later backend might add; the client does not know it.
    const later = { ...makeSection('keywords'), key: 'later' as SectionKey, title: 'Later' };
    const future = { ...makeSection('keywords'), key: 'future' as SectionKey, title: 'Future' };

    await renderWithProviders(
      <SectionNav sections={[later, makeSection('budget'), future, makeSection('keywords')]} />,
    );

    expect(linkSummary()).toEqual([
      ['Keywords to adjust 0', '#section-keywords'],
      ['Budget changes 0', '#section-budget'],
      ['Later 0', '#section-later'],
      ['Future 0', '#section-future'],
    ]);
  });

  it('links a repeated key once, to its first section', async () => {
    const first = sectionWithItems('budget', [itemWith(21), itemWith(22)]);
    const repeat = { ...sectionWithItems('budget', [itemWith(23)]), title: 'Budget again' };

    await renderWithProviders(<SectionNav sections={[first, repeat]} />);

    expect(linkSummary()).toEqual([['Budget changes 2', '#section-budget']]);
  });

  it('falls back to the section key when the title is blank or not a string', async () => {
    const untitled = makeSection('creative', { title: '   ' });
    const padded = makeSection('keywords', { title: `  ${FIXTURE_SECTION_TITLES.keywords}  ` });
    // A malformed response: the type promises a string, the component guards anyway.
    const nullTitle = makeSection('budget', { title: null as unknown as string });

    await renderWithProviders(<SectionNav sections={[untitled, padded, nullTitle]} />);

    expect(linkSummary()).toEqual([
      ['Keywords to adjust 0', '#section-keywords'],
      ['budget 0', '#section-budget'],
      ['creative 0', '#section-creative'],
    ]);
  });
});
