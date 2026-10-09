/**
 * Contract tests for `Tabs`, the design-system sub-list switch (AAP 0.4.2, status GAP;
 * `../GAPS.md`, binding procedure step 6), which follows the WAI-ARIA tabs pattern with automatic
 * activation (AAP 0.1.1, WCAG 2.2 AA keyboard operation).
 *
 * They pin what feature code and assistive technology rely on, so a later Blitzy OS binding that
 * changes the wrapper's internals must still pass them:
 * - one `tablist` named by `label`, one `tab` per item named by its label and count;
 * - exactly one tab selected (`aria-selected`), alone in the Tab sequence, controlling the one
 *   visible `tabpanel`, which is labelled by its tab; hidden panels stay mounted;
 * - ArrowRight and ArrowLeft wrap, Home and End jump, focus and selection move together, and
 *   modified or unrelated keys are left to the browser;
 * - uncontrolled by default, controlled through `selectedKey` and `onChange`, with an unknown
 *   key falling back to the first tab, and nothing rendered for an empty `tabs` array.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Tabs, type TabItem } from './Tabs';

/** The Keywords section's three sub-lists, with counts including a zero and an absent count. */
const KEYWORD_TABS: readonly TabItem[] = [
  { key: 'block', label: 'Block', count: 3, content: <p>Block list</p> },
  { key: 'expand', label: 'Expand', count: 0, content: <p>Expand list</p> },
  { key: 'review', label: 'Review', content: <p>Review list</p> },
];

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
      expect(allTabs().map((tab) => tab.textContent)).toEqual(['Block 3', 'Expand 0', 'Review']);
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
      // Hidden panels keep their content mounted.
      expect(panels.map((panel) => panel.textContent)).toEqual([
        'Block list',
        'Expand list',
        'Review list',
      ]);
      expect(screen.getByRole('tabpanel', { name: 'Block 3' })).toBe(panels[0]);
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

      expect(allTabs().map((tab) => tab.textContent)).toEqual(['Review', 'Expand 0', 'Block 3']);
      expect(selectedStates()).toEqual(['false', 'true', 'false']);

      tabAt(1).focus();
      fireEvent.keyDown(tabAt(1), { key: 'ArrowRight' });
      expect(document.activeElement).toBe(tabAt(2));
      expect(visiblePanelText()).toBe('Block list');
    });
  });
});
