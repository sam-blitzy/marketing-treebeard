/**
 * Contract tests for `Tabs`, the design-system sub-list switch (AAP 0.4.2, status GAP;
 * `../GAPS.md`, binding procedure step 6), which follows the WAI-ARIA tabs pattern with automatic
 * activation (AAP 0.1.1, WCAG 2.2 AA keyboard operation).
 *
 * They pin what feature code and assistive technology rely on, so a later Blitzy OS binding that
 * changes the wrapper's internals must still pass them:
 * - one `tablist` named by `label`, one `tab` per item named by its label and count, the count
 *   digit-grouped as `formatCount` groups it ("Block 18,002");
 * - each label rendered visibly and once more as an `aria-hidden` sizer that reserves its
 *   semibold width, out of the tab's name, with a tab's content the same selected or not;
 * - exactly one tab selected (`aria-selected`), alone in the Tab sequence, controlling the one
 *   visible `tabpanel`, which is labelled by its tab; every panel element exists from the start
 *   (named by the tab's `id` when given), its content mounts on its tab's first selection and
 *   then stays mounted;
 * - ArrowRight and ArrowLeft wrap, Home and End jump, focus and selection move together, and
 *   modified or unrelated keys are left to the browser; the row scrolls the tab the keys reach
 *   into view, showing the start of a tab wider than the visible row;
 * - uncontrolled by default, controlled through `selectedKey` and `onChange`, with an unknown
 *   key falling back to the first tab, and nothing rendered for an empty `tabs` array.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Tabs, type TabItem } from './Tabs';

/** The Keywords section's three sub-lists, with counts including a zero and an absent count. */
const KEYWORD_TABS: readonly TabItem[] = [
  { key: 'block', label: 'Block', count: 3, content: <p>Block list</p> },
  { key: 'expand', label: 'Expand', count: 0, content: <p>Expand list</p> },
  { key: 'review', label: 'Review', content: <p>Review list</p> },
];

/** A tab keyed and labelled `key`, whose panel reads "{key} list". */
function tabFixture(key: string): TabItem {
  return { key, label: key, content: <p>{`${key} list`}</p> };
}

/** The tab elements in display order. */
function allTabs(): HTMLElement[] {
  return screen.getAllByRole('tab');
}

/** The tab at a display index, failing the test when it is missing. */
function tabAt(index: number): HTMLElement {
  const tab = allTabs()[index];
  if (tab === undefined) {
    throw new Error(`No tab at index ${String(index)}`);
  }
  return tab;
}

/**
 * The text `element` presents: its text content without the `aria-hidden` subtrees, which
 * leaves out the hidden copy of each tab label that reserves its semibold width.
 */
function presentedText(element: HTMLElement): string | null {
  const copy = element.cloneNode(true) as HTMLElement;
  for (const hidden of copy.querySelectorAll('[aria-hidden="true"]')) {
    hidden.remove();
  }
  return copy.textContent;
}

/** The `aria-selected` value of every tab, in display order. */
function selectedStates(): (string | null)[] {
  return allTabs().map((tab) => tab.getAttribute('aria-selected'));
}

/** The visible panel's text: the panel of the selected tab. */
function visiblePanelText(): string | null {
  return screen.getByRole('tabpanel').textContent;
}

describe('Tabs', () => {
  describe('roles, names and ARIA wiring', () => {
    it('renders a named tablist with one tab per item, named by label and count', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);

      const list = screen.getByRole('tablist', { name: 'Keyword actions' });
      expect(list.getAttribute('aria-label')).toBe('Keyword actions');
      expect(allTabs().map(presentedText)).toEqual(['Block 3', 'Expand 0', 'Review']);
      expect(screen.getByRole('tab', { name: 'Block 3' })).toBe(tabAt(0));
      expect(screen.getByRole('tab', { name: 'Expand 0' })).toBe(tabAt(1));
      expect(screen.getByRole('tab', { name: 'Review' })).toBe(tabAt(2));
      expect(tabAt(1).querySelector('.ds-tabs__count')?.textContent).toBe('0');
      expect(tabAt(2).querySelector('.ds-tabs__count')).toBeNull();
      for (const tab of allTabs()) {
        expect(tab.tagName).toBe('BUTTON');
        expect(tab.getAttribute('type')).toBe('button');
      }
    });

    it('groups the digits of each count as formatCount does, in its text and tab name', () => {
      const counts: readonly [number, string][] = [
        [18002, '18,002'],
        [20262, '20,262'],
        [1000, '1,000'],
        [1234567, '1,234,567'],
        [999, '999'],
        [0, '0'],
      ];
      render(
        <Tabs
          label="Keyword actions"
          tabs={counts.map(([count], index) => ({
            key: `tab-${String(index)}`,
            label: 'Block',
            count,
            content: <p>{`List ${String(index)}`}</p>,
          }))}
        />,
      );

      counts.forEach(([, text], index) => {
        expect(presentedText(tabAt(index))).toBe(`Block ${text}`);
        expect(tabAt(index).querySelector('.ds-tabs__count')?.textContent).toBe(text);
      });
      expect(screen.getByRole('tab', { name: 'Block 18,002' })).toBe(tabAt(0));
      expect(screen.getByRole('tab', { name: 'Block 0' })).toBe(tabAt(5));
    });

    it.each([
      ['NaN', Number.NaN],
      ['Infinity', Number.POSITIVE_INFINITY],
    ])('renders the count %s as "unavailable", never as the raw value', (_name, count) => {
      render(
        <Tabs
          label="Keyword actions"
          tabs={[{ key: 'block', label: 'Block', count, content: <p>Block list</p> }]}
        />,
      );

      expect(tabAt(0).querySelector('.ds-tabs__count')?.textContent).toBe('unavailable');
      expect(screen.getByRole('tab', { name: 'Block unavailable' })).toBe(tabAt(0));
    });

    it('reserves each label width with one hidden copy that stays out of the tab name', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);

      allTabs().forEach((tab, index) => {
        const expected = KEYWORD_TABS[index]?.label;
        const slot = tab.querySelector('.ds-tabs__label');
        // The visible label, then the sizer that reserves its semibold width, both the label.
        expect(Array.from(slot?.children ?? [], (child) => child.className)).toEqual([
          'ds-tabs__label-text',
          'ds-tabs__label-sizer',
        ]);
        const [text, sizer] = Array.from(slot?.children ?? []);
        expect(text?.textContent).toBe(expected);
        expect(text?.hasAttribute('aria-hidden')).toBe(false);
        expect(sizer?.textContent).toBe(expected);
        expect(sizer?.getAttribute('aria-hidden')).toBe('true');
        // The sizer is the only content of the tab hidden from assistive technology.
        const hidden = tab.querySelectorAll('[aria-hidden="true"]');
        expect(hidden).toHaveLength(1);
        expect(hidden[0]).toBe(sizer);
      });
      // Every name is still the label and count, with no trace of the hidden copy.
      expect(screen.getByRole('tab', { name: 'Block 3' })).toBe(tabAt(0));
      expect(screen.getByRole('tab', { name: 'Expand 0' })).toBe(tabAt(1));
      expect(screen.getByRole('tab', { name: 'Review' })).toBe(tabAt(2));
      expect(screen.queryByRole('tab', { name: /Block\s*Block|Review\s*Review/u })).toBeNull();
    });

    it('owns its tabs directly, inside a scroller that adds no semantics', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);

      const list = screen.getByRole('tablist', { name: 'Keyword actions' });
      expect(Array.from(list.children)).toEqual(allTabs());
      // The row scrolls inside a plain wrapper, so no role or name sits between list and tabs.
      const scroller = list.parentElement;
      expect(scroller?.className).toBe('ds-tabs__scroller');
      expect(scroller?.getAttributeNames()).toEqual(['class']);
      expect(scroller?.children).toHaveLength(1);
    });

    it('renders the same tab content whether the tab is selected or not', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);
      const contentBefore = allTabs().map((tab) => tab.innerHTML);

      fireEvent.click(tabAt(1));

      expect(selectedStates()).toEqual(['false', 'true', 'false']);
      // Only the tabs' own state attributes changed; their content, sizers included, did not.
      expect(allTabs().map((tab) => tab.innerHTML)).toEqual(contentBefore);
    });

    it('selects the first tab by default and keeps only it in the Tab sequence', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);

      expect(selectedStates()).toEqual(['true', 'false', 'false']);
      expect(allTabs().map((tab) => tab.getAttribute('tabindex'))).toEqual(['0', '-1', '-1']);
    });

    it('ties every tab to its own panel and shows only the selected panel', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);

      const panels = screen.getAllByRole('tabpanel', { hidden: true });
      expect(panels).toHaveLength(3);
      allTabs().forEach((tab, index) => {
        const panel = panels[index];
        expect(panel).toBeDefined();
        expect(tab.getAttribute('aria-controls')).toBe(panel?.id);
        expect(panel?.getAttribute('aria-labelledby')).toBe(tab.id);
        expect(panel?.getAttribute('tabindex')).toBe('0');
      });
      expect(panels.map((panel) => panel.hidden)).toEqual([false, true, true]);
      // Only the selected panel is exposed to assistive technology.
      const visiblePanels = screen.getAllByRole('tabpanel');
      expect(visiblePanels).toHaveLength(1);
      expect(visiblePanels[0]).toBe(panels[0]);
      expect(visiblePanels[0]?.textContent).toBe('Block list');
      // Every panel element exists, so each aria-controls resolves, but a tab never selected has
      // not mounted its content.
      expect(panels.map((panel) => panel.textContent)).toEqual(['Block list', '', '']);
      for (const tab of allTabs()) {
        expect(document.getElementById(tab.getAttribute('aria-controls') ?? '')).not.toBeNull();
      }
      expect(screen.getByRole('tabpanel', { name: 'Block 3' })).toBe(panels[0]);
    });

    it('mounts a panel on its first selection and keeps it mounted, hidden, afterwards', () => {
      const mounts = vi.fn<(name: string) => void>();
      /** Panel content that reports each mount and keeps a local count. */
      function Probe({ name }: { name: string }) {
        const [clicks, setClicks] = useState(0);
        useEffect(() => {
          mounts(name);
        }, [name]);
        return (
          <button type="button" onClick={() => setClicks((count) => count + 1)}>
            {`${name} ${String(clicks)}`}
          </button>
        );
      }
      render(
        <Tabs
          label="Keyword actions"
          tabs={[
            { key: 'block', label: 'Block', content: <Probe name="block" /> },
            { key: 'expand', label: 'Expand', content: <Probe name="expand" /> },
            { key: 'review', label: 'Review', content: <Probe name="review" /> },
          ]}
        />,
      );
      expect(mounts.mock.calls).toEqual([['block']]);

      fireEvent.click(screen.getByRole('button', { name: 'block 0' }));
      fireEvent.click(tabAt(1));
      expect(mounts.mock.calls).toEqual([['block'], ['expand']]);
      expect(visiblePanelText()).toBe('expand 0');
      // Block stays mounted while hidden, so its local state survives the switch.
      const panels = screen.getAllByRole('tabpanel', { hidden: true });
      expect(panels.map((panel) => panel.textContent)).toEqual(['block 1', 'expand 0', '']);

      fireEvent.click(tabAt(0));
      expect(visiblePanelText()).toBe('block 1');
      expect(mounts.mock.calls).toEqual([['block'], ['expand']]);
    });

    it('mounts the panel a controlled parent selects without a click', () => {
      const { rerender } = render(
        <Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="block" />,
      );
      expect(
        screen.getAllByRole('tabpanel', { hidden: true }).map((panel) => panel.textContent),
      ).toEqual(['Block list', '', '']);

      rerender(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="review" />);

      expect(selectedStates()).toEqual(['false', 'false', 'true']);
      expect(visiblePanelText()).toBe('Review list');
      expect(
        screen.getAllByRole('tabpanel', { hidden: true }).map((panel) => panel.textContent),
      ).toEqual(['Block list', '', 'Review list']);
    });

    it('names a panel by its tab id, which the tab controls, even before its content mounts', () => {
      render(
        <Tabs
          label="Keyword actions"
          tabs={[
            { ...tabFixture('block'), id: 'group-keywords-block' },
            { ...tabFixture('expand'), id: 'group-keywords-expand' },
            { ...tabFixture('review'), id: '' },
          ]}
        />,
      );

      const expand = document.getElementById('group-keywords-expand');
      expect(expand?.getAttribute('role')).toBe('tabpanel');
      expect(expand?.hidden).toBe(true);
      expect(expand?.textContent).toBe('');
      expect(tabAt(1).getAttribute('aria-controls')).toBe('group-keywords-expand');
      expect(expand?.getAttribute('aria-labelledby')).toBe(tabAt(1).id);
      expect(tabAt(0).getAttribute('aria-controls')).toBe('group-keywords-block');
      // An empty id falls back to the generated one, which still resolves.
      const reviewPanelId = tabAt(2).getAttribute('aria-controls') ?? '';
      expect(reviewPanelId).not.toBe('');
      expect(document.getElementById(reviewPanelId)?.getAttribute('role')).toBe('tabpanel');

      fireEvent.keyDown(tabAt(0), { key: 'ArrowRight' });
      expect(document.activeElement).toBe(tabAt(1));
      expect(expand?.hidden).toBe(false);
      expect(expand?.textContent).toBe('expand list');
    });

    it('gives two tab sets on one page distinct ids', () => {
      render(
        <>
          <Tabs label="First" tabs={KEYWORD_TABS} />
          <Tabs label="Second" tabs={KEYWORD_TABS} />
        </>,
      );

      const ids = allTabs().map((tab) => tab.id);
      expect(new Set(ids).size).toBe(ids.length);
    });

    it('renders nothing for an empty tabs array', () => {
      const { container } = render(<Tabs label="Keyword actions" tabs={[]} />);

      expect(container.innerHTML).toBe('');
      expect(screen.queryByRole('tablist')).toBeNull();
    });
  });

  describe('pointer selection', () => {
    it('selects a clicked tab, shows its panel and reports its key (uncontrolled)', () => {
      const onChange = vi.fn<(key: string) => void>();
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} onChange={onChange} />);

      fireEvent.click(tabAt(2));

      expect(onChange).toHaveBeenCalledWith('review');
      expect(selectedStates()).toEqual(['false', 'false', 'true']);
      expect(allTabs().map((tab) => tab.getAttribute('tabindex'))).toEqual(['-1', '-1', '0']);
      expect(visiblePanelText()).toBe('Review list');
    });

    it('works without an onChange handler', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);

      fireEvent.click(tabAt(1));

      expect(selectedStates()).toEqual(['false', 'true', 'false']);
      expect(visiblePanelText()).toBe('Expand list');
    });
  });

  describe('keyboard', () => {
    it('moves focus and selection with ArrowRight, wrapping from the last tab to the first', () => {
      const onChange = vi.fn<(key: string) => void>();
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} onChange={onChange} />);
      tabAt(0).focus();

      expect(fireEvent.keyDown(tabAt(0), { key: 'ArrowRight' })).toBe(false);
      expect(document.activeElement).toBe(tabAt(1));
      expect(selectedStates()).toEqual(['false', 'true', 'false']);
      expect(visiblePanelText()).toBe('Expand list');

      fireEvent.keyDown(tabAt(1), { key: 'ArrowRight' });
      expect(document.activeElement).toBe(tabAt(2));
      expect(selectedStates()).toEqual(['false', 'false', 'true']);

      fireEvent.keyDown(tabAt(2), { key: 'ArrowRight' });
      expect(document.activeElement).toBe(tabAt(0));
      expect(selectedStates()).toEqual(['true', 'false', 'false']);
      expect(onChange.mock.calls).toEqual([['expand'], ['review'], ['block']]);
    });

    it('moves with ArrowLeft, wrapping from the first tab to the last', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);
      tabAt(0).focus();

      expect(fireEvent.keyDown(tabAt(0), { key: 'ArrowLeft' })).toBe(false);
      expect(document.activeElement).toBe(tabAt(2));
      expect(selectedStates()).toEqual(['false', 'false', 'true']);

      fireEvent.keyDown(tabAt(2), { key: 'ArrowLeft' });
      expect(document.activeElement).toBe(tabAt(1));
      expect(selectedStates()).toEqual(['false', 'true', 'false']);
      expect(visiblePanelText()).toBe('Expand list');
    });

    it('jumps to the last tab with End and to the first with Home', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);
      tabAt(0).focus();

      expect(fireEvent.keyDown(tabAt(0), { key: 'End' })).toBe(false);
      expect(document.activeElement).toBe(tabAt(2));
      expect(selectedStates()).toEqual(['false', 'false', 'true']);

      expect(fireEvent.keyDown(tabAt(2), { key: 'Home' })).toBe(false);
      expect(document.activeElement).toBe(tabAt(0));
      expect(selectedStates()).toEqual(['true', 'false', 'false']);
    });

    it.each(['Enter', ' ', 'a', 'ArrowDown', 'Tab'])('leaves the %j key to the browser', (key) => {
      const onChange = vi.fn<(key: string) => void>();
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} onChange={onChange} />);
      tabAt(0).focus();

      expect(fireEvent.keyDown(tabAt(0), { key })).toBe(true);

      expect(onChange).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(tabAt(0));
      expect(selectedStates()).toEqual(['true', 'false', 'false']);
    });

    it.each([
      ['Alt', { altKey: true }],
      ['Control', { ctrlKey: true }],
      ['Meta', { metaKey: true }],
    ])('leaves %s+arrow combinations to the browser', (_modifier, modifiers) => {
      const onChange = vi.fn<(key: string) => void>();
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} onChange={onChange} />);
      tabAt(0).focus();

      expect(fireEvent.keyDown(tabAt(0), { key: 'ArrowRight', ...modifiers })).toBe(true);

      expect(onChange).not.toHaveBeenCalled();
      expect(selectedStates()).toEqual(['true', 'false', 'false']);
    });

    describe('scrolling the focused tab into view', () => {
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
        Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
      });

      it('scrolls the row just far enough to show each tab the keys move to', () => {
        render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);
        tabAt(0).focus();

        fireEvent.keyDown(tabAt(0), { key: 'End' });
        fireEvent.keyDown(tabAt(2), { key: 'ArrowLeft' });

        expect(scrollIntoView.mock.contexts).toEqual([tabAt(2), tabAt(1)]);
        expect(scrollIntoView.mock.calls).toEqual([
          [{ block: 'nearest', inline: 'nearest' }],
          [{ block: 'nearest', inline: 'nearest' }],
        ]);
        expect(document.activeElement).toBe(tabAt(1));
      });

      it('shows the start of a tab wider than the visible row', () => {
        render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);
        const scroller = screen.getByRole('tablist').parentElement;
        if (scroller === null) {
          throw new Error('The tab list has no scroller');
        }
        Object.defineProperty(scroller, 'clientWidth', { configurable: true, value: 296 });
        Object.defineProperty(tabAt(1), 'offsetWidth', { configurable: true, value: 297 });
        Object.defineProperty(tabAt(2), 'offsetWidth', { configurable: true, value: 296 });
        tabAt(0).focus();

        fireEvent.keyDown(tabAt(0), { key: 'ArrowRight' });
        fireEvent.keyDown(tabAt(1), { key: 'ArrowRight' });

        expect(scrollIntoView.mock.contexts).toEqual([tabAt(1), tabAt(2)]);
        expect(scrollIntoView.mock.calls).toEqual([
          [{ block: 'nearest', inline: 'start' }],
          [{ block: 'nearest', inline: 'nearest' }],
        ]);
      });

      it('leaves pointer selection to the browser', () => {
        render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);

        fireEvent.click(tabAt(2));

        expect(selectedStates()).toEqual(['false', 'false', 'true']);
        expect(scrollIntoView).not.toHaveBeenCalled();
      });
    });

    it('navigates from the selected tab when the key event comes from the tablist itself', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="expand" />);

      fireEvent.keyDown(screen.getByRole('tablist'), { key: 'ArrowRight' });

      expect(document.activeElement).toBe(tabAt(2));
    });
  });

  describe('controlled mode', () => {
    it('shows selectedKey and changes only when the parent passes the new key back', () => {
      const onChange = vi.fn<(key: string) => void>();
      const { rerender } = render(
        <Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="expand" onChange={onChange} />,
      );
      expect(selectedStates()).toEqual(['false', 'true', 'false']);
      expect(visiblePanelText()).toBe('Expand list');

      fireEvent.click(tabAt(2));

      expect(onChange).toHaveBeenCalledWith('review');
      expect(selectedStates()).toEqual(['false', 'true', 'false']);

      rerender(
        <Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="review" onChange={onChange} />,
      );
      expect(selectedStates()).toEqual(['false', 'false', 'true']);
      expect(visiblePanelText()).toBe('Review list');
    });

    it('reports an arrow-key selection and waits for the parent to adopt it', () => {
      const onChange = vi.fn<(key: string) => void>();
      const { rerender } = render(
        <Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="expand" onChange={onChange} />,
      );
      tabAt(1).focus();

      expect(fireEvent.keyDown(tabAt(1), { key: 'ArrowRight' })).toBe(false);

      expect(onChange.mock.calls).toEqual([['review']]);
      // Focus follows the key at once; the selection stays with the parent's key.
      expect(document.activeElement).toBe(tabAt(2));
      expect(selectedStates()).toEqual(['false', 'true', 'false']);
      expect(visiblePanelText()).toBe('Expand list');

      rerender(
        <Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="review" onChange={onChange} />,
      );

      expect(selectedStates()).toEqual(['false', 'false', 'true']);
      expect(allTabs().map((tab) => tab.getAttribute('tabindex'))).toEqual(['-1', '-1', '0']);
      expect(document.activeElement).toBe(tabAt(2));
      expect(visiblePanelText()).toBe('Review list');
    });

    it('selects the first tab when selectedKey matches no tab', () => {
      render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="missing" />);

      expect(selectedStates()).toEqual(['true', 'false', 'false']);
      expect(visiblePanelText()).toBe('Block list');
    });

    it('navigates from the focused tab while the parent has not adopted the last change', () => {
      const onChange = vi.fn<(key: string) => void>();
      render(
        <Tabs label="Keyword actions" tabs={KEYWORD_TABS} selectedKey="block" onChange={onChange} />,
      );
      tabAt(0).focus();

      fireEvent.keyDown(tabAt(0), { key: 'ArrowRight' });
      expect(document.activeElement).toBe(tabAt(1));
      expect(selectedStates()).toEqual(['true', 'false', 'false']);

      fireEvent.keyDown(tabAt(1), { key: 'ArrowRight' });
      expect(document.activeElement).toBe(tabAt(2));
      expect(onChange.mock.calls).toEqual([['expand'], ['review']]);
    });
  });

  describe('changing tabs', () => {
    it('falls back to the first tab when the selected tab is removed', () => {
      const { rerender } = render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);
      fireEvent.click(tabAt(2));
      expect(visiblePanelText()).toBe('Review list');

      rerender(<Tabs label="Keyword actions" tabs={KEYWORD_TABS.slice(0, 2)} />);

      expect(selectedStates()).toEqual(['true', 'false']);
      expect(visiblePanelText()).toBe('Block list');
    });

    it('keeps the selection on its key when the tabs are reordered', () => {
      const { rerender } = render(<Tabs label="Keyword actions" tabs={KEYWORD_TABS} />);
      fireEvent.click(tabAt(1));

      rerender(<Tabs label="Keyword actions" tabs={[...KEYWORD_TABS].reverse()} />);

      expect(allTabs().map(presentedText)).toEqual(['Review', 'Expand 0', 'Block 3']);
      expect(selectedStates()).toEqual(['false', 'true', 'false']);

      tabAt(1).focus();
      fireEvent.keyDown(tabAt(1), { key: 'ArrowRight' });
      expect(document.activeElement).toBe(tabAt(2));
      expect(visiblePanelText()).toBe('Block list');
    });
  });
});
