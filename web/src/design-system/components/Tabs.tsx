/**
 * `Tabs`: the product's sub-list switch (AAP 0.4.2, status GAP).
 *
 * The Keywords section switches between its Block, Expand and Review sub-lists with this
 * component, each tab showing its item count (AAP 0.5.5). It implements the WAI-ARIA Authoring
 * Practices tabs pattern with automatic activation (AAP 0.1.1, WCAG 2.2 AA keyboard operation):
 *
 * - one `tablist` named by the `label` prop, holding one `tab` per item;
 * - only the selected tab is in the Tab sequence (`tabindex="0"`, the others `-1`), so Tab moves
 *   from the tab list straight into the visible panel;
 * - ArrowRight and ArrowLeft move to the next and previous tab, wrapping at either end, and Home
 *   and End move to the first and last tab. Focus and selection move together;
 * - every tab controls its own `tabpanel` (`aria-controls` / `aria-labelledby`). Every panel
 *   element is always rendered, so each `aria-controls` resolves and a panel given an `id` is
 *   always a valid in-page anchor. Only the selected panel is visible; the others carry
 *   `hidden`, so they leave both the screen and the accessibility tree;
 * - a panel's content mounts the first time its tab is selected and then stays mounted, hidden,
 *   while another tab is selected. A tab that is never selected costs nothing to render, and
 *   what a panel holds in local state (an open disclosure, a half-typed note) survives a switch
 *   away and back.
 *
 * No headless library is used (AAP 0.4.5). The styling lives in `Tabs.css`, whose class names are
 * the contract this component renders: `ds-tabs`, `ds-tabs__list`, `ds-tabs__tab`,
 * `ds-tabs__label`, `ds-tabs__count` and `ds-tabs__panel`. The label sits in its own span so a
 * long unbroken label wraps inside its tab instead of widening the row. The selected tab is
 * styled from `aria-selected`, so the visual state can never disagree with the state announced
 * to assistive technology.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `Tabs.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Tabs.css';

import { useId, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';

/**
 * One tab and the panel it shows.
 *
 * GAP: bind to Blitzy OS on source arrival. This is a product-level shape, not a Blitzy OS
 * type; on binding, it maps onto the Blitzy OS tab item.
 */
export interface TabItem {
  /**
   * Identifies the tab within its `Tabs`: the value of `selectedKey` and of the `onChange`
   * argument. Keys must be unique within one `tabs` array and stable across renders.
   */
  key: string;
  /**
   * The visible tab label, for example `"Block"`. It forms the tab's accessible name. A long
   * unbroken label, such as an identifier, wraps inside its tab and is never truncated.
   */
  label: ReactNode;
  /**
   * Optional item count shown beside the label, for example the number of items in a sub-list.
   * It is part of the tab's accessible name ("Block 3"). `0` is rendered; omit the prop to
   * show no count.
   */
  count?: number;
  /**
   * The panel content shown while this tab is selected. It mounts the first time the tab is
   * selected and stays mounted, hidden, while another tab is selected.
   */
  content: ReactNode;
  /**
   * Optional id of this tab's panel element, which the tab names in `aria-controls`. It must be
   * unique on the page. Give it when something links to the panel, such as the in-page anchor
   * `#group-keywords-block`: the panel element is always rendered, so the anchor resolves even
   * before the content first mounts. Omitted or empty, the id is derived from `useId`.
   */
  id?: string;
}

/**
 * Props of {@link Tabs}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because
 * feature code must not restyle a wrapper (AAP 0.4.4).
 *
 * The component is uncontrolled by default: it starts on the first tab and tracks the
 * selection itself. Passing `selectedKey` makes it controlled: the parent owns the selection,
 * receives every requested change through `onChange` and shows it by passing the new key back.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS tabs component.
 */
export interface TabsProps {
  /** Accessible name of the tab list, for example `"Keyword actions"`. */
  label: string;
  /** The tabs in display order. An empty array renders nothing. */
  tabs: readonly TabItem[];
  /**
   * The selected tab's key, which makes the component controlled. A key that matches no tab
   * selects the first tab. Omit it to let the component track the selection itself.
   */
  selectedKey?: string;
  /**
   * Called with a tab's key whenever the user selects it, by pointer or keyboard. In controlled
   * mode the selection changes only once the parent passes that key back as `selectedKey`.
   */
  onChange?: (key: string) => void;
}

/**
 * The tab a navigation key moves to, as an index into a list of `count` tabs, or `undefined`
 * when the key is not a tab-list navigation key and must be left to the browser.
 *
 * @param key - the `KeyboardEvent.key` value
 * @param current - index of the tab the key was pressed on
 * @param count - number of tabs, at least 1
 */
function targetIndex(key: string, current: number, count: number): number | undefined {
  switch (key) {
    case 'ArrowRight':
      return (current + 1) % count;
    case 'ArrowLeft':
      return (current - 1 + count) % count;
    case 'Home':
      return 0;
    case 'End':
      return count - 1;
    default:
      return undefined;
  }
}

/**
 * Accessible tabs: a labelled tab list over one visible panel, operated by pointer and keyboard
 * per the WAI-ARIA tabs pattern with automatic activation.
 *
 * Renders `null` when `tabs` is empty. Element ids derive from `useId`, so several `Tabs` on one
 * page never collide; a tab's `id`, when given, names its panel instead. Every panel element is
 * rendered from the start, and its content from the first time its tab is selected.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <Tabs
 *   label="Keyword actions"
 *   tabs={[
 *     { key: 'block', label: 'Block', count: blockItems.length, content: <BlockList /> },
 *     { key: 'expand', label: 'Expand', count: expandItems.length, content: <ExpandList /> },
 *     { key: 'review', label: 'Review', count: reviewItems.length, content: <ReviewList /> },
 *   ]}
 * />
 *
 * // Controlled: the parent owns the selection.
 * <Tabs label="Keyword actions" tabs={tabs} selectedKey={view} onChange={setView} />
 * ```
 */
export function Tabs({ label, tabs, selectedKey, onChange }: TabsProps) {
  const baseId = useId();
  const [internalKey, setInternalKey] = useState<string | undefined>(tabs[0]?.key);
  // The keys of every tab selected so far, whose panel content is mounted.
  const [mountedKeys, setMountedKeys] = useState<readonly string[]>([]);
  // Each rendered tab registers its button here, so keyboard navigation can move focus to the
  // tab it selects. Keyed by tab key, so the map stays correct when tabs are reordered.
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());

  // The requested key wins only when it names a rendered tab; otherwise the first tab is
  // selected, so exactly one tab is always selected and exactly one panel visible.
  const requestedKey = selectedKey ?? internalKey;
  const activeKey = tabs.some((tab) => tab.key === requestedKey) ? requestedKey : tabs[0]?.key;

  // Record a newly selected tab while rendering, React's pattern for state derived from the
  // previous render: the selection can change without `select` (a controlled parent's new
  // `selectedKey`, a removed tab's fallback), and React re-renders at once with the key added.
  if (activeKey !== undefined && !mountedKeys.includes(activeKey)) {
    setMountedKeys([...mountedKeys, activeKey]);
  }

  if (tabs.length === 0) {
    return null;
  }

  const tabId = (index: number) => `${baseId}-tab-${index}`;
  const panelId = (tab: TabItem, index: number) =>
    tab.id === undefined || tab.id === '' ? `${baseId}-panel-${index}` : tab.id;

  const select = (key: string) => {
    // Uncontrolled: track the selection here. Controlled: the parent decides through onChange.
    if (selectedKey === undefined) {
      setInternalKey(key);
    }
    onChange?.(key);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // Modified keys keep their browser meaning (Alt+ArrowLeft is "back", for example).
    if (event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }
    // Navigate from the tab that holds focus. With automatic activation that is the selected
    // tab, except in controlled mode while the parent has not yet adopted the last change.
    const focusedIndex = tabs.findIndex((tab) => tabRefs.current.get(tab.key) === event.target);
    const current =
      focusedIndex >= 0 ? focusedIndex : tabs.findIndex((tab) => tab.key === activeKey);
    const nextIndex = targetIndex(event.key, Math.max(current, 0), tabs.length);
    if (nextIndex === undefined) {
      return;
    }
    const next = tabs[nextIndex];
    if (next === undefined) {
      return;
    }
    event.preventDefault();
    select(next.key);
    tabRefs.current.get(next.key)?.focus();
  };

  return (
    <div className="ds-tabs">
      <div className="ds-tabs__list" role="tablist" aria-label={label} onKeyDown={onKeyDown}>
        {tabs.map((tab, index) => {
          const selected = tab.key === activeKey;
          return (
            <button
              key={tab.key}
              ref={(node) => {
                if (node === null) {
                  tabRefs.current.delete(tab.key);
                } else {
                  tabRefs.current.set(tab.key, node);
                }
              }}
              type="button"
              role="tab"
              id={tabId(index)}
              className="ds-tabs__tab"
              aria-selected={selected}
              aria-controls={panelId(tab, index)}
              tabIndex={selected ? 0 : -1}
              onClick={() => {
                select(tab.key);
              }}
            >
              <span className="ds-tabs__label">{tab.label}</span>
              {/* The space between label and count keeps the accessible name "Block 3". */}
              {tab.count !== undefined && (
                <>
                  {' '}
                  <span className="ds-tabs__count">{tab.count}</span>
                </>
              )}
            </button>
          );
        })}
      </div>
      {tabs.map((tab, index) => {
        const selected = tab.key === activeKey;
        return (
          <div
            key={tab.key}
            role="tabpanel"
            id={panelId(tab, index)}
            aria-labelledby={tabId(index)}
            tabIndex={0}
            hidden={!selected}
            className="ds-tabs__panel"
          >
            {/* Mounted from the first selection on; never before (see the module overview). */}
            {selected || mountedKeys.includes(tab.key) ? tab.content : null}
          </div>
        );
      })}
    </div>
  );
}
