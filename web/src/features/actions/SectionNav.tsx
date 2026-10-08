/**
 * `SectionNav`: the `/actions` section navigation, anchor links with item counts for the five
 * sections (AAP 0.5.5).
 *
 * It renders one design-system `SideNav` landmark named "Sections", holding one in-page link per
 * section in the user's order: Keywords to adjust, Campaigns to pause or launch, Budget changes,
 * Copy / creative changes, White papers ({@link SECTION_ORDER}). Each link points at the section
 * region `SectionFrame` anchors (`#section-{key}`, see `sectionAnchorId`) and shows how many
 * cards that section holds now.
 *
 * Presentation rules this file keeps:
 * - The count is what the page shows. An item hidden live by feedback given since the run
 *   (`current.hidden_by`: dismiss, snooze or done; AAP 0.3.2) is not counted, because the
 *   section counts it under "Not shown" instead of rendering it. A held item still counts: it
 *   renders, collapsed behind "Held until discrepancies are resolved" (AAP 0.5.5). Both rules
 *   come from `SectionFrame`'s own `isHidden`, so the nav and the section never disagree.
 * - Every title and key comes from the API (`Section.title`, `Section.key`); the only literal is
 *   the landmark's accessible name.
 * - Built only on the design-system index (AAP 0.4.4): no raw elements, no inline styles and no
 *   stylesheet. The plain `<a href>` `SideNav` renders by default is the right element for an
 *   in-page anchor, so no router link is involved.
 *
 * @packageDocumentation
 */
import type { ReactElement } from 'react';

import { SECTION_ORDER } from '../../api/types';
import type { Section } from '../../api/types';
import { SideNav } from '../../design-system';
import type { SideNavItem } from '../../design-system';
import { isHidden, sectionAnchorId } from './sections/SectionFrame';

/**
 * Accessible name of the section navigation landmark. It tells this `<nav>` apart from the app's
 * primary navigation on the same page.
 */
const NAV_LABEL = 'Sections';

/**
 * Position of each known section key in {@link SECTION_ORDER}. A key the client does not know,
 * such as one added to the backend later, has no entry and sorts after every known key.
 */
const ORDER_RANK: ReadonlyMap<string, number> = new Map<string, number>(
  SECTION_ORDER.map((key, index) => [key, index] as const),
);

/**
 * Sort position of a section key: its index in {@link SECTION_ORDER}, or
 * `SECTION_ORDER.length` for an unknown key, so every unknown key ties after the known ones.
 *
 * @param key - the section key, as the API returned it
 * @returns the sort position
 */
function orderRank(key: string): number {
  return ORDER_RANK.get(key) ?? SECTION_ORDER.length;
}

/**
 * The sections in navigation order: the known keys in {@link SECTION_ORDER}, then any unknown
 * key in the order the API returned it. A key the API did not return is simply absent.
 *
 * Only the first section for a key is kept. The API returns each key once; should a key repeat,
 * its anchor `section-{key}` can only target one region, and the page renders the first section
 * found for a key, so a second link would duplicate the first link's destination and its React
 * list key.
 *
 * The sort is stable (`Array.prototype.toSorted`), which is what keeps unknown keys in API
 * order. The input is never mutated.
 *
 * @param sections - the sections, as the API returned them
 * @returns a new array in navigation order, one section per key
 */
function orderSections(sections: readonly Section[]): Section[] {
  const seen = new Set<string>();
  const unique: Section[] = [];
  for (const section of sections) {
    if (!seen.has(section.key)) {
      seen.add(section.key);
      unique.push(section);
    }
  }
  return unique.toSorted((left, right) => orderRank(left.key) - orderRank(right.key));
}

/**
 * The link text of a section: its API title, trimmed. A title that is blank or not a string
 * falls back to the section key, exactly as `SectionFrame` titles the section's heading, so the
 * link and the heading it points at always read the same and a link is never empty.
 *
 * @param section - the section
 * @returns the non-empty link text
 */
function sectionLabel(section: Section): string {
  const title: unknown = section.title;
  if (typeof title === 'string') {
    const trimmed = title.trim();
    if (trimmed !== '') {
      return trimmed;
    }
  }
  return section.key;
}

/**
 * How many of a section's items the page shows now: every item, in every group, that feedback
 * given since the run does not hide (`current.hidden_by` is `null`; AAP 0.3.2).
 *
 * Held items are counted, because the section renders them collapsed behind its held disclosure
 * rather than leaving them out (AAP 0.5.5).
 *
 * @param section - the section, as `GET /recommendations` returned it with the item overlay
 * @returns the number of items the section shows, `0` for a section without items
 *
 * @example
 * ```ts
 * // A group with a visible, a held and a dismissed item:
 * visibleItemCount(section); // 2
 * ```
 */
export function visibleItemCount(section: Section): number {
  let count = 0;
  for (const group of section.groups) {
    for (const item of group.items) {
      if (!isHidden(item)) {
        count += 1;
      }
    }
  }
  return count;
}

/** Props of {@link SectionNav}. */
export interface SectionNavProps {
  /**
   * The page's sections, as `GET /recommendations` returned them (`RecommendationsPage.sections`).
   * The API always returns the five sections in {@link SECTION_ORDER}; the nav orders them itself
   * all the same, so it never depends on that.
   */
  sections: readonly Section[];
}

/**
 * The section navigation of `/actions` (AAP 0.5.5): a `SideNav` landmark named "Sections" with
 * one anchor link per section, in {@link SECTION_ORDER}, each labelled with the section's API
 * title and followed by its {@link visibleItemCount}. A count of `0` is shown, not omitted.
 *
 * Sections are ordered by {@link SECTION_ORDER}; a key the API did not return gets no link, and
 * a key the client does not know is linked after the known ones, in API order. Each link's
 * `href` is `#section-{key}`, the anchor of the region `SectionFrame` renders for that section.
 *
 * The component holds no state, has no effects and fetches nothing; its output depends only on
 * its props.
 *
 * @param props - see {@link SectionNavProps}
 * @returns the section navigation landmark
 *
 * @example
 * ```tsx
 * <SectionNav sections={page.sections} />
 * // <nav aria-label="Sections"> … <a href="#section-keywords">Keywords to adjust 3</a> …
 * ```
 */
export function SectionNav({ sections }: SectionNavProps): ReactElement {
  const items: SideNavItem[] = orderSections(sections).map((section) => ({
    key: section.key,
    label: sectionLabel(section),
    href: `#${sectionAnchorId(section.key)}`,
    count: visibleItemCount(section),
  }));

  return <SideNav label={NAV_LABEL} items={items} />;
}
