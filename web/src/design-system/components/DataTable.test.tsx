/**
 * Contract tests for `DataTable`, the design-system data table (AAP 0.4.2, status GAP;
 * `../GAPS.md`, binding procedure step 6).
 *
 * They pin what feature code and assistive technology rely on, so a later Blitzy OS binding that
 * changes the wrapper's internals must still pass them:
 * - every table has a caption (AAP 0.5.5) and one non-empty name shared with its focusable
 *   scrolling `region`: a text caption names both, a rich caption needs `captionLabel`;
 * - column headers are `columnheader` cells, the identifying column renders `rowheader` cells,
 *   and numeric columns (`align: 'end'`) align their header and cells to the end;
 * - default cells show only string and number properties, `render` overrides them, and an empty
 *   table shows `emptyMessage` across every column;
 * - expandable rows: one native toggle button per row with `aria-expanded` and `aria-controls`
 *   pointing at its detail row, which is hidden and empty until expanded, remembered by row key;
 *   an expandable table's region carries the expandable modifier and an expanded row's content
 *   sits in the detail content wrapper, while a table that cannot expand renders neither;
 * - keyboard operation, exercised with Tab, Shift+Tab, Enter and Space: the region and then each
 *   toggle are stops in the Tab order, a collapsed detail row adds none and an expanded row's
 *   focusable content follows its toggle, and Enter or Space on a toggle expands and collapses
 *   its row while focus stays on it. jsdom performs no sequential navigation or key activation
 *   of its own, so the helpers dispatch the key events and simulate the browser's default action
 *   (the focus move, or the native button's click) only when no handler prevented the key;
 * - fixed column widths: a table that does not expand and whose every column has a width on the
 *   scale carries the fixed modifier and one `col` per column after its caption, holding the
 *   column's steps, with its headers, cells and name unchanged; without widths, with only some,
 *   with one off the scale or on an expandable table, it renders exactly as before, with no
 *   column group;
 * - an unnamed or column-less table renders the static "Table unavailable" state naming what is
 *   missing, and no table or region.
 */
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { DataTable, type DataTableColumn, type DataTableColumnWidth } from './DataTable';

/** A white-paper row, with properties of every kind the default cell must handle. */
interface AssetRow {
  slug: string;
  asset: string;
  spend: number;
  mql: number | null;
  ql?: number;
  live: boolean;
  meta: { owner: string };
}

/** Two assets: one with every metric, one with a zero spend, a null MQL and no QL. */
const ROWS: readonly AssetRow[] = [
  { slug: 'guide', asset: 'Buyer guide', spend: 1200, mql: 4, ql: 2, live: true, meta: { owner: 'a' } },
  { slug: 'report', asset: 'State report', spend: 0, mql: null, live: false, meta: { owner: 'b' } },
];

/**
 * The white-paper columns: an identifying row-header column, a rendered currency column, two
 * numeric default cells, a boolean and an object property, and a key no row carries.
 */
const COLUMNS: readonly DataTableColumn<AssetRow>[] = [
  { key: 'asset', header: 'Asset', isRowHeader: true },
  { key: 'spend', header: 'Spend', align: 'end', render: (row) => `EUR ${row.spend.toFixed(2)}` },
  { key: 'mql', header: 'MQL (First meeting booked)', align: 'end' },
  { key: 'ql', header: 'Qualified Lead', align: 'end' },
  { key: 'live', header: 'Live' },
  { key: 'meta', header: 'Owner' },
  { key: 'missing', header: 'Missing', align: 'start' },
];

/** The table's row key: the asset slug. */
const slugOf = (row: AssetRow): string => row.slug;

/** The cells of a body row in display order, its row header included. */
function cellsOf(row: HTMLElement): HTMLElement[] {
  return Array.from(row.querySelectorAll<HTMLElement>(':scope > th, :scope > td'));
}

/** The cell texts of a body row, its row header included. */
function cellTexts(row: HTMLElement): (string | null)[] {
  return cellsOf(row).map((cell) => cell.textContent);
}

/** The element at an index, failing the test when it is missing. */
function at<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) {
    throw new Error(`No item at index ${String(index)}`);
  }
  return item;
}

/** The props a test may override on the white-paper table, which always has a text caption. */
interface AssetTableOverrides {
  caption?: string;
  columns?: readonly DataTableColumn<AssetRow>[];
  rows?: readonly AssetRow[];
  emptyMessage?: ReactNode;
  renderExpanded?: (row: AssetRow) => ReactNode;
  expandLabel?: (row: AssetRow, expanded: boolean) => string;
}

/** Renders the white-paper table captioned "White papers · EUR", with the given overrides. */
function renderAssets({
  caption = 'White papers · EUR',
  columns = COLUMNS,
  rows = ROWS,
  ...rest
}: AssetTableOverrides = {}) {
  return render(
    <DataTable caption={caption} columns={columns} rows={rows} getRowKey={slugOf} {...rest} />,
  );
}

/**
 * Whether an element is a stop in the simplified Tab model these fixtures need, judged
 * independently of the component: a non-negative `tabIndex`, not a disabled control, a hidden
 * input or a link without `href` (which reports `tabIndex` 0 yet takes no focus), and not inside
 * a `hidden`, `inert` or `display: none` element. Browser focus rules no fixture here exercises,
 * such as `visibility: hidden`, a radio group's single stop and shadow roots, are not modelled.
 */
function isTabStop(element: HTMLElement): boolean {
  if (element.tabIndex < 0 || element.matches(':disabled, input[type="hidden"]')) {
    return false;
  }
  if (
    (element instanceof HTMLAnchorElement || element instanceof HTMLAreaElement) &&
    !element.hasAttribute('href') &&
    !element.hasAttribute('tabindex')
  ) {
    return false;
  }
  if (element.closest('[hidden], [inert]') !== null) {
    return false;
  }
  for (let node: HTMLElement | null = element; node !== null; node = node.parentElement) {
    if (getComputedStyle(node).display === 'none') {
      return false;
    }
  }
  return true;
}

/**
 * The modelled Tab order of the rendered fixture's {@link isTabStop} stops: those with a positive
 * `tabIndex` first, in ascending order and tree order among equals, then those with `tabIndex` 0
 * in tree order.
 */
function tabSequence(): HTMLElement[] {
  const stops = Array.from(document.body.querySelectorAll('*')).filter(
    (element): element is HTMLElement => element instanceof HTMLElement && isTabStop(element),
  );
  const positive = stops
    .filter((stop) => stop.tabIndex > 0)
    .sort((left, right) => left.tabIndex - right.tabIndex);
  return [...positive, ...stops.filter((stop) => stop.tabIndex === 0)];
}

/**
 * The stop Tab (or Shift+Tab) moves focus to from an element in the simplified Tab model.
 *
 * From the body the first stop is next (the last with Shift). From a stop it is the following
 * stop in {@link tabSequence} (the preceding one with Shift). From a focused element that is not
 * a stop it is the first stop in that sequence after it in tree order (the last before it).
 *
 * @param from - the focused element
 * @param backwards - `true` for Shift+Tab
 * @returns the next stop, or `null` when the sequence has none in that direction
 */
function nextTabStop(from: Element, backwards: boolean): HTMLElement | null {
  const sequence = tabSequence();
  if (from === document.body || from === document.documentElement) {
    return (backwards ? sequence.at(-1) : sequence[0]) ?? null;
  }
  const index = sequence.findIndex((stop) => stop === from);
  if (index >= 0) {
    return sequence[index + (backwards ? -1 : 1)] ?? null;
  }
  const relation = backwards ? Node.DOCUMENT_POSITION_PRECEDING : Node.DOCUMENT_POSITION_FOLLOWING;
  const inDirection = (stop: HTMLElement): boolean =>
    (from.compareDocumentPosition(stop) & relation) !== 0;
  return (backwards ? sequence.findLast(inDirection) : sequence.find(inDirection)) ?? null;
}

/**
 * Presses Tab (or Shift+Tab) on the focused element (the body when nothing has focus).
 *
 * jsdom performs no sequential navigation, so when no handler prevented the keydown the
 * browser's step is simulated: focus moves to {@link nextTabStop}. The keyup then goes to the
 * element that has focus.
 *
 * @param shiftKey - `true` for Shift+Tab
 * @returns the element that received focus, or `null` when a handler prevented the keydown or no
 *   stop follows in that direction; focus then stays where it was
 */
function pressTab(shiftKey = false): HTMLElement | null {
  const target = document.activeElement ?? document.body;
  const next = fireEvent.keyDown(target, { key: 'Tab', code: 'Tab', shiftKey })
    ? nextTabStop(target, shiftKey)
    : null;
  if (next !== null) {
    act(() => {
      next.focus();
    });
  }
  fireEvent.keyUp(document.activeElement ?? document.body, { key: 'Tab', code: 'Tab', shiftKey });
  return next;
}

/**
 * Whether a key can activate an element as it activates a native button: an enabled `<button>`
 * or an enabled `<input>` of type button, submit or reset.
 */
function isActivatableButton(element: Element): element is HTMLButtonElement | HTMLInputElement {
  const native =
    element instanceof HTMLButtonElement ||
    (element instanceof HTMLInputElement && ['button', 'submit', 'reset'].includes(element.type));
  return native && !element.matches(':disabled');
}

/**
 * Presses Enter on the focused element (the body when nothing has focus).
 *
 * jsdom turns no key into a click, so the browser's activation is simulated: when neither the
 * keydown nor the keypress was prevented and the element is a native button, it is clicked.
 * Other elements receive the key events alone. The keyup follows in every case, on the element
 * that then has focus.
 *
 * @returns `true` when the element was activated
 */
function pressEnter(): boolean {
  const target = document.activeElement ?? document.body;
  let activated = false;
  if (
    fireEvent.keyDown(target, { key: 'Enter', code: 'Enter' }) &&
    fireEvent.keyPress(target, { key: 'Enter', code: 'Enter', charCode: 13 }) &&
    isActivatableButton(target)
  ) {
    act(() => {
      target.click();
    });
    activated = true;
  }
  fireEvent.keyUp(document.activeElement ?? document.body, { key: 'Enter', code: 'Enter' });
  return activated;
}

/**
 * Presses Space on the focused element (the body when nothing has focus).
 *
 * A native button activates on the Space keyup, and a prevented keydown or keyup cancels it.
 * jsdom turns no key into a click, so that activation is simulated: when neither event was
 * prevented and the element is a native button, it is clicked after the keyup.
 *
 * @returns `true` when the element was activated
 */
function pressSpace(): boolean {
  const target = document.activeElement ?? document.body;
  const downAllowed = fireEvent.keyDown(target, { key: ' ', code: 'Space' });
  const upAllowed = fireEvent.keyUp(target, { key: ' ', code: 'Space' });
  if (downAllowed && upAllowed && isActivatableButton(target)) {
    act(() => {
      target.click();
    });
    return true;
  }
  return false;
}

describe('DataTable', () => {
  describe('naming and the scrolling region', () => {
    it('names the table by its text caption and the focusable region by the same caption', () => {
      renderAssets();

      const table = screen.getByRole('table', { name: 'White papers · EUR' });
      const caption = table.querySelector('caption');
      expect(caption?.textContent).toBe('White papers · EUR');
      expect(table.hasAttribute('aria-label')).toBe(false);

      const region = screen.getByRole('region', { name: 'White papers · EUR' });
      expect(region.getAttribute('tabindex')).toBe('0');
      expect(region.getAttribute('aria-labelledby')).toBe(caption?.id);
      expect(region.hasAttribute('aria-label')).toBe(false);
      expect(region.contains(table)).toBe(true);

      region.focus();
      expect(document.activeElement).toBe(region);
    });

    it('names the table and region by captionLabel for a rich caption', () => {
      render(
        <DataTable
          caption={
            <>
              Evidence · <abbr title="Euro">EUR</abbr>
            </>
          }
          captionLabel="Evidence · EUR"
          columns={COLUMNS}
          rows={ROWS}
          getRowKey={slugOf}
        />,
      );

      const table = screen.getByRole('table', { name: 'Evidence · EUR' });
      expect(table.getAttribute('aria-label')).toBe('Evidence · EUR');
      expect(table.querySelector('caption abbr')?.getAttribute('title')).toBe('Euro');
      const region = screen.getByRole('region', { name: 'Evidence · EUR' });
      expect(region.getAttribute('aria-label')).toBe('Evidence · EUR');
      expect(region.hasAttribute('aria-labelledby')).toBe(false);
    });

    it('accepts a rich caption given as an array with content', () => {
      render(
        <DataTable
          caption={['', <span key="t">Variants</span>]}
          captionLabel="Variants"
          columns={COLUMNS}
          rows={ROWS}
          getRowKey={slugOf}
        />,
      );

      expect(screen.getByRole('table', { name: 'Variants' })).toBeDefined();
    });

    it('accepts a nested iterable caption whose content sits deeper', () => {
      render(
        <DataTable
          caption={[[null, false], ['  ', ['Nested']]]}
          captionLabel="Nested"
          columns={COLUMNS}
          rows={ROWS}
          getRowKey={slugOf}
        />,
      );

      expect(screen.getByRole('table', { name: 'Nested' }).querySelector('caption')?.textContent).toBe(
        '  Nested',
      );
    });

    it.each([
      ['a number', 0, '0'],
      ['a bigint', 12n, '12'],
    ])('treats %s caption as text that names the table', (_kind, caption, name) => {
      render(<DataTable caption={caption} columns={COLUMNS} rows={ROWS} getRowKey={slugOf} />);

      expect(screen.getByRole('table', { name })).toBeDefined();
      expect(screen.getByRole('region', { name })).toBeDefined();
    });

    it('gives two tables on one page distinct caption ids', () => {
      render(
        <>
          <DataTable caption="First" columns={COLUMNS} rows={ROWS} getRowKey={slugOf} />
          <DataTable caption="Second" columns={COLUMNS} rows={ROWS} getRowKey={slugOf} />
        </>,
      );

      expect(screen.getByRole('region', { name: 'First' }).getAttribute('aria-labelledby')).not.toBe(
        screen.getByRole('region', { name: 'Second' }).getAttribute('aria-labelledby'),
      );
    });
  });

  describe('headers, cells and alignment', () => {
    it('renders column headers in order with scope col and end alignment for numbers', () => {
      renderAssets();

      const headers = screen.getAllByRole('columnheader');
      expect(headers.map((header) => header.textContent)).toEqual([
        'Asset',
        'Spend',
        'MQL (First meeting booked)',
        'Qualified Lead',
        'Live',
        'Owner',
        'Missing',
      ]);
      for (const header of headers) {
        expect(header.tagName).toBe('TH');
        expect(header.getAttribute('scope')).toBe('col');
      }
      const alignments = headers.map((header) =>
        header.classList.contains('ds-datatable__cell--end') ? 'end' : 'start',
      );
      expect(alignments).toEqual(['start', 'end', 'end', 'end', 'start', 'start', 'start']);
      expect(at(headers, 0).classList.contains('ds-datatable__cell--start')).toBe(true);
    });

    it('renders the identifying column as row headers and other columns as cells', () => {
      renderAssets();

      const rowHeaders = screen.getAllByRole('rowheader');
      expect(rowHeaders.map((cell) => cell.textContent)).toEqual(['Buyer guide', 'State report']);
      for (const cell of rowHeaders) {
        expect(cell.tagName).toBe('TH');
        expect(cell.getAttribute('scope')).toBe('row');
      }
      expect(screen.getByRole('row', { name: /Buyer guide/ })).toBeDefined();
    });

    it('shows strings and numbers, renders through render, and leaves other values empty', () => {
      renderAssets();

      const bodyRows = screen.getAllByRole('row').slice(1);
      expect(bodyRows).toHaveLength(2);
      // asset (row header), spend (render), mql, ql, live (boolean), meta (object), missing key.
      expect(cellTexts(at(bodyRows, 0))).toEqual(['Buyer guide', 'EUR 1200.00', '4', '2', '', '', '']);
      // A zero renders as 0; null and an absent property render as empty cells.
      expect(cellTexts(at(bodyRows, 1))).toEqual(['State report', 'EUR 0.00', '', '', '', '', '']);
    });

    it('aligns numeric body cells to the end and text cells to the start', () => {
      renderAssets();

      const firstRow = at(screen.getAllByRole('row'), 1);
      const cells = cellsOf(firstRow);
      expect(cells.map((cell) => cell.classList.contains('ds-datatable__cell--end'))).toEqual([
        false,
        true,
        true,
        true,
        false,
        false,
        false,
      ]);
      // The asset row header and the other text cells carry the explicit start class.
      expect(cells.map((cell) => cell.classList.contains('ds-datatable__cell--start'))).toEqual([
        true,
        false,
        false,
        false,
        true,
        true,
        true,
      ]);
    });

    it('renders a cell for a row that is not an object as empty', () => {
      render(
        <DataTable<string>
          caption="Terms"
          columns={[{ key: 'length', header: 'Length' }]}
          rows={['alpha']}
          getRowKey={(row) => row}
        />,
      );

      expect(cellTexts(at(screen.getAllByRole('row'), 1))).toEqual(['']);
    });
  });

  describe('empty rows', () => {
    it('shows the default message in one cell spanning every column', () => {
      renderAssets({ rows: [] });

      const cell = screen.getByRole('cell', { name: 'No rows' });
      expect(cell.getAttribute('colspan')).toBe(String(COLUMNS.length));
      expect(screen.getAllByRole('row')).toHaveLength(2);
    });

    it('shows a custom empty message and spans the toggle column too when expandable', () => {
      renderAssets({
        rows: [],
        emptyMessage: 'No white papers in this window',
        renderExpanded: () => 'detail',
      });

      const cell = screen.getByRole('cell', { name: 'No white papers in this window' });
      expect(cell.getAttribute('colspan')).toBe(String(COLUMNS.length + 1));
      expect(screen.queryByRole('button')).toBeNull();
    });
  });

  describe('expandable rows', () => {
    /** Detail content that names its row, so the test can see which row expanded. */
    const renderDetail = (row: AssetRow): ReactNode => <p>Creatives of {row.asset}</p>;

    /** The detail row a toggle controls, found through its `aria-controls`. */
    const detailOf = (toggle: HTMLElement): HTMLElement | null =>
      document.getElementById(toggle.getAttribute('aria-controls') ?? '');

    /**
     * Asserts a default-labelled toggle and its detail row: expanded and showing exactly
     * `content`, or, when `content` is `null`, collapsed with the detail row hidden and empty.
     */
    function expectRow(toggle: HTMLElement, content: string | null): void {
      const expanded = content !== null;
      expect(toggle.getAttribute('aria-expanded')).toBe(String(expanded));
      expect(toggle.textContent).toBe(expanded ? 'Hide details' : 'Show details');
      const detail = detailOf(toggle);
      expect(detail?.hidden).toBe(!expanded);
      expect(detail?.textContent).toBe(content ?? '');
    }

    it('adds a visually hidden Details header and one collapsed toggle per row', () => {
      renderAssets({ renderExpanded: renderDetail });

      const headers = screen.getAllByRole('columnheader');
      expect(headers).toHaveLength(COLUMNS.length + 1);
      const detailsHeader = at(headers, 0);
      expect(detailsHeader.textContent).toBe('Details');
      expect(detailsHeader.querySelector('.ds-visually-hidden')?.textContent).toBe('Details');

      const toggles = screen.getAllByRole('button', { name: 'Show details' });
      expect(toggles).toHaveLength(2);
      for (const toggle of toggles) {
        expect(toggle.tagName).toBe('BUTTON');
        expect(toggle.getAttribute('type')).toBe('button');
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
        const detail = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
        expect(detail?.tagName).toBe('TR');
        expect(detail?.hidden).toBe(true);
        expect(detail?.textContent).toBe('');
      }
      expect(screen.queryByText(/Creatives of/)).toBeNull();
    });

    it('expands and collapses a row from its toggle', () => {
      const renderExpanded = vi.fn(renderDetail);
      renderAssets({ renderExpanded });
      const toggle = at(screen.getAllByRole('button', { name: 'Show details' }), 0);
      const detailId = toggle.getAttribute('aria-controls') ?? '';

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(toggle.textContent).toBe('Hide details');
      const detail = document.getElementById(detailId);
      expect(detail?.hidden).toBe(false);
      expect(detail?.textContent).toBe('Creatives of Buyer guide');
      const detailCell = detail?.querySelector('td');
      expect(detailCell?.getAttribute('colspan')).toBe(String(COLUMNS.length + 1));
      expect(renderExpanded).toHaveBeenCalledWith(ROWS[0]);
      expect(screen.getAllByRole('button', { name: 'Show details' })).toHaveLength(1);

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(toggle.textContent).toBe('Show details');
      expect(document.getElementById(detailId)?.hidden).toBe(true);
      expect(document.getElementById(detailId)?.textContent).toBe('');
    });

    it('marks the region expandable and wraps expanded content in the detail content wrapper', () => {
      renderAssets({ renderExpanded: renderDetail });
      const region = screen.getByRole('region', { name: 'White papers · EUR' });
      expect(region.className).toBe('ds-datatable__region ds-datatable__region--expandable');
      expect(document.querySelector('.ds-datatable__detail-content')).toBeNull();

      const toggle = at(screen.getAllByRole('button', { name: 'Show details' }), 0);
      fireEvent.click(toggle);

      const detailCell = document
        .getElementById(toggle.getAttribute('aria-controls') ?? '')
        ?.querySelector(':scope > td.ds-datatable__detail-cell');
      const wrappers = Array.from(detailCell?.children ?? []);
      expect(wrappers).toHaveLength(1);
      const wrapper = at(wrappers, 0);
      expect(wrapper.tagName).toBe('DIV');
      expect(wrapper.className).toBe('ds-datatable__detail-content');
      expect(wrapper.innerHTML).toBe('<p>Creatives of Buyer guide</p>');
      expect(document.querySelectorAll('.ds-datatable__detail-content')).toHaveLength(1);

      fireEvent.click(toggle);

      expect(document.querySelector('.ds-datatable__detail-content')).toBeNull();
      expect(region.className).toBe('ds-datatable__region ds-datatable__region--expandable');
    });

    it('marks the region of an expandable table expandable even when it has no rows', () => {
      renderAssets({ rows: [], renderExpanded: renderDetail });

      expect(screen.getByRole('region', { name: 'White papers · EUR' }).className).toBe(
        'ds-datatable__region ds-datatable__region--expandable',
      );
      expect(document.querySelector('.ds-datatable__detail-content')).toBeNull();
    });

    it('puts the region and then each toggle in the Tab order, with no stop in a collapsed detail row', () => {
      renderAssets({ renderExpanded: renderDetail });
      const region = screen.getByRole('region', { name: 'White papers · EUR' });
      const toggles = screen.getAllByRole('button', { name: 'Show details' });
      const first = at(toggles, 0);
      const second = at(toggles, 1);
      expect(region.getAttribute('tabindex')).toBe('0');
      expect(document.activeElement).toBe(document.body);

      expect(pressTab()).toBe(region);
      expect(document.activeElement).toBe(region);
      expect(pressTab()).toBe(first);
      expect(document.activeElement).toBe(first);
      // The first detail row is collapsed, so Tab goes straight to the next row's toggle.
      expect(pressTab()).toBe(second);
      expect(document.activeElement).toBe(second);
      expect(pressTab()).toBeNull();
      expect(document.activeElement).toBe(second);

      expect(pressTab(true)).toBe(first);
      expect(document.activeElement).toBe(first);
      expect(pressTab(true)).toBe(region);
      expect(document.activeElement).toBe(region);
      expectRow(first, null);
      expectRow(second, null);
    });

    it('expands and collapses the focused toggle with Enter and keeps focus on it', () => {
      renderAssets({ renderExpanded: renderDetail });
      const toggles = screen.getAllByRole('button', { name: 'Show details' });
      const first = at(toggles, 0);
      const second = at(toggles, 1);
      expect(pressTab()).toBe(screen.getByRole('region', { name: 'White papers · EUR' }));
      expect(pressTab()).toBe(first);

      expect(pressEnter()).toBe(true);

      expectRow(first, 'Creatives of Buyer guide');
      expectRow(second, null);
      expect(document.activeElement).toBe(first);

      expect(pressEnter()).toBe(true);

      expectRow(first, null);
      expectRow(second, null);
      expect(document.activeElement).toBe(first);
    });

    it('expands and collapses a toggle reached by Tab with Space and keeps focus on it', () => {
      renderAssets({ renderExpanded: renderDetail });
      const toggles = screen.getAllByRole('button', { name: 'Show details' });
      const first = at(toggles, 0);
      const second = at(toggles, 1);
      expect(pressTab()).toBe(screen.getByRole('region', { name: 'White papers · EUR' }));
      expect(pressTab()).toBe(first);
      expect(pressTab()).toBe(second);

      expect(pressSpace()).toBe(true);

      expectRow(second, 'Creatives of State report');
      expectRow(first, null);
      expect(document.activeElement).toBe(second);

      expect(pressSpace()).toBe(true);

      expectRow(second, null);
      expectRow(first, null);
      expect(document.activeElement).toBe(second);
    });

    it("puts an expanded row's focusable content after its toggle in the Tab order, and drops it on collapse", () => {
      renderAssets({
        renderExpanded: (row) => <a href={`#creatives-${row.slug}`}>Creatives of {row.asset}</a>,
      });
      const toggles = screen.getAllByRole('button', { name: 'Show details' });
      const first = at(toggles, 0);
      const second = at(toggles, 1);
      expect(pressTab()).toBe(screen.getByRole('region', { name: 'White papers · EUR' }));
      expect(pressTab()).toBe(first);
      expect(pressEnter()).toBe(true);
      expectRow(first, 'Creatives of Buyer guide');
      const link = screen.getByRole('link', { name: 'Creatives of Buyer guide' });
      expect(detailOf(first)?.contains(link)).toBe(true);

      expect(pressTab()).toBe(link);
      expect(pressTab()).toBe(second);
      expect(pressTab(true)).toBe(link);
      expect(pressTab(true)).toBe(first);

      expect(pressEnter()).toBe(true);

      expectRow(first, null);
      expect(screen.queryByRole('link')).toBeNull();
      expect(document.activeElement).toBe(first);
      expect(pressTab()).toBe(second);
      expect(pressTab(true)).toBe(first);
    });

    it('leaves the row collapsed when a handler prevents the default of Enter or Space', () => {
      render(
        <div
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
            }
          }}
        >
          <DataTable
            caption="White papers · EUR"
            columns={COLUMNS}
            rows={ROWS}
            getRowKey={slugOf}
            renderExpanded={renderDetail}
          />
        </div>,
      );
      const toggles = screen.getAllByRole('button', { name: 'Show details' });
      const first = at(toggles, 0);
      const second = at(toggles, 1);
      expect(pressTab()).toBe(screen.getByRole('region', { name: 'White papers · EUR' }));
      expect(pressTab()).toBe(first);

      expect(pressEnter()).toBe(false);

      expectRow(first, null);
      expect(document.activeElement).toBe(first);
      expect(pressTab()).toBe(second);

      expect(pressSpace()).toBe(false);

      expectRow(second, null);
      expect(document.activeElement).toBe(second);
      // A pointer click still expands the row, so only the keys' default action was blocked.
      fireEvent.click(second);
      expectRow(second, 'Creatives of State report');
    });

    it('names each toggle with expandLabel, from its row and state', () => {
      const expandLabel = vi.fn(
        (row: AssetRow, expanded: boolean) => `${expanded ? 'Hide' : 'Show'} creatives for ${row.asset}`,
      );
      renderAssets({ renderExpanded: renderDetail, expandLabel });

      const toggle = screen.getByRole('button', { name: 'Show creatives for State report' });
      expect(screen.getByRole('button', { name: 'Show creatives for Buyer guide' })).toBeDefined();

      fireEvent.click(toggle);

      expect(screen.getByRole('button', { name: 'Hide creatives for State report' })).toBe(toggle);
      expect(expandLabel).toHaveBeenCalledWith(ROWS[1], true);
    });

    it('keeps a row expanded by its key when the rows are re-rendered in another order', () => {
      const expandLabel = (row: AssetRow, expanded: boolean): string =>
        `${expanded ? 'Hide' : 'Show'} ${row.asset}`;
      const { rerender } = render(
        <DataTable
          caption="White papers · EUR"
          columns={COLUMNS}
          rows={ROWS}
          getRowKey={slugOf}
          renderExpanded={renderDetail}
          expandLabel={expandLabel}
        />,
      );
      fireEvent.click(screen.getByRole('button', { name: 'Show State report' }));

      rerender(
        <DataTable
          caption="White papers · EUR"
          columns={COLUMNS}
          rows={[...ROWS].reverse()}
          getRowKey={slugOf}
          renderExpanded={renderDetail}
          expandLabel={expandLabel}
        />,
      );

      expect(screen.getAllByRole('rowheader').map((cell) => cell.textContent)).toEqual([
        'State report',
        'Buyer guide',
      ]);
      const open = screen.getByRole('button', { name: 'Hide State report' });
      expect(open.getAttribute('aria-expanded')).toBe('true');
      expect(document.getElementById(open.getAttribute('aria-controls') ?? '')?.textContent).toBe(
        'Creatives of State report',
      );
      expect(
        screen.getByRole('button', { name: 'Show Buyer guide' }).getAttribute('aria-expanded'),
      ).toBe('false');
    });

    it('renders no toggles or detail rows without renderExpanded', () => {
      renderAssets();

      expect(screen.queryByRole('button')).toBeNull();
      expect(document.querySelector('.ds-datatable__detail-row')).toBeNull();
      expect(screen.getAllByRole('columnheader')).toHaveLength(COLUMNS.length);
      expect(screen.getByRole('region', { name: 'White papers · EUR' }).className).toBe(
        'ds-datatable__region',
      );
      expect(document.querySelector('.ds-datatable__region--expandable')).toBeNull();
      expect(document.querySelector('.ds-datatable__detail-content')).toBeNull();
    });
  });

  describe('fixed column widths', () => {
    /** A width on the scale for each of the white-paper columns, in their order. */
    const WIDTHS: readonly DataTableColumnWidth[] = [12, 7, 8, 8, 4, 6, 16];

    /** The white-paper columns, each with its width from {@link WIDTHS}. */
    const SIZED_COLUMNS: readonly DataTableColumn<AssetRow>[] = COLUMNS.map((column, index) => ({
      ...column,
      width: at(WIDTHS, index),
    }));

    /** The table captioned "White papers · EUR". */
    function assetTable(): HTMLElement {
      return screen.getByRole('table', { name: 'White papers · EUR' });
    }

    /** The tag names of a table's children, in order. */
    function childTags(table: HTMLElement): string[] {
      return Array.from(table.children, (child) => child.tagName);
    }

    /** The width steps every `<col>` of a table holds, in order. */
    function colSteps(table: HTMLElement): string[] {
      return Array.from(table.querySelectorAll<HTMLElement>('col'), (col) =>
        col.style.getPropertyValue('--ds-datatable-col-steps'),
      );
    }

    /** Asserts the automatic layout: the plain table class and no column group. */
    function expectAutomaticLayout(table: HTMLElement): void {
      expect(table.className).toBe('ds-datatable');
      expect(table.querySelector('colgroup, col')).toBeNull();
    }

    it('keeps the automatic layout, with no column group, when no column has a width', () => {
      renderAssets();

      const table = assetTable();
      expectAutomaticLayout(table);
      expect(childTags(table)).toEqual(['CAPTION', 'THEAD', 'TBODY']);
    });

    it('lays the table out at fixed widths, one col per column, when every column has one', () => {
      renderAssets({ columns: SIZED_COLUMNS });

      const table = assetTable();
      expect(table.className).toBe('ds-datatable ds-datatable--fixed');
      // The column group follows the caption and precedes the header row, as HTML requires.
      expect(childTags(table)).toEqual(['CAPTION', 'COLGROUP', 'THEAD', 'TBODY']);
      const cols = Array.from(table.querySelectorAll<HTMLElement>('colgroup > col'));
      expect(cols.map((col) => col.className)).toEqual(
        Array.from(COLUMNS, () => 'ds-datatable__col'),
      );
      expect(colSteps(table)).toEqual(['12', '7', '8', '8', '4', '6', '16']);
      // The inline style holds the component-private width property and nothing else.
      expect(cols.every((col) => col.style.length === 1)).toBe(true);
    });

    it('keeps the headers, cells, alignment and name of the table unchanged', () => {
      renderAssets({ columns: SIZED_COLUMNS });

      const headers = screen.getAllByRole('columnheader');
      expect(headers.map((header) => header.textContent)).toEqual(
        COLUMNS.map((column) => column.header),
      );
      const ends = headers.map((header) => header.classList.contains('ds-datatable__cell--end'));
      expect(ends).toEqual([false, true, true, true, false, false, false]);
      expect(screen.getAllByRole('rowheader').map((cell) => cell.textContent)).toEqual([
        'Buyer guide',
        'State report',
      ]);
      expect(cellTexts(at(screen.getAllByRole('row'), 1))).toEqual([
        'Buyer guide',
        'EUR 1200.00',
        '4',
        '2',
        '',
        '',
        '',
      ]);
      expect(screen.getByRole('region', { name: 'White papers · EUR' }).className).toBe(
        'ds-datatable__region',
      );
    });

    it('gives tables with the same widths the same columns, whatever their rows', () => {
      render(
        <>
          <DataTable caption="First" columns={SIZED_COLUMNS} rows={ROWS} getRowKey={slugOf} />
          <DataTable caption="Second" columns={SIZED_COLUMNS} rows={[]} getRowKey={slugOf} />
        </>,
      );

      const first = screen.getByRole('table', { name: 'First' });
      const second = screen.getByRole('table', { name: 'Second' });
      expect(colSteps(second)).toEqual(colSteps(first));
      expect(second.className).toBe('ds-datatable ds-datatable--fixed');
      // The empty-state cell still spans every column.
      expect(screen.getByRole('cell', { name: 'No rows' }).getAttribute('colspan')).toBe(
        String(COLUMNS.length),
      );
    });

    it('ignores the widths when only some columns have one', () => {
      renderAssets({
        columns: COLUMNS.map((column, index) =>
          index === 0 ? { ...column, width: 12 as const } : column,
        ),
      });

      expectAutomaticLayout(assetTable());
    });

    it('ignores the widths of an expandable table, whose toggle column has none', () => {
      renderAssets({ columns: SIZED_COLUMNS, renderExpanded: () => 'detail' });

      expectAutomaticLayout(assetTable());
      expect(screen.getAllByRole('button')).toHaveLength(ROWS.length);
    });

    it.each([
      ['a fraction', 7.5],
      ['a step below the scale', 3],
      ['a step above the scale', 17],
      ['a numeric string', '7'],
    ])('ignores the widths when one column has %s from an untyped caller', (_kind, width) => {
      renderAssets({
        columns: SIZED_COLUMNS.map((column, index) =>
          index === 1 ? { ...column, width: width as unknown as DataTableColumnWidth } : column,
        ),
      });

      expectAutomaticLayout(assetTable());
    });
  });

  describe('Table unavailable', () => {
    /** Asserts the configuration state with its reason, and that no table or region exists. */
    function expectUnavailable(reason: string): void {
      expect(screen.getByText('Table unavailable')).toBeDefined();
      expect(screen.getByText(reason)).toBeDefined();
      expect(screen.queryByRole('table')).toBeNull();
      expect(screen.queryByRole('region')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    }

    it.each([
      ['an empty caption', ''],
      ['a whitespace caption', '   '],
    ])('renders it for %s', (_kind, caption) => {
      renderAssets({ caption });

      expectUnavailable('This table has no caption to name it.');
    });

    it.each([
      ['an empty array', []],
      ['an array of blank entries', ['', '  ', null, false, undefined]],
    ])('renders it for a rich caption that is %s', (_kind, caption) => {
      render(
        <DataTable caption={caption} captionLabel="Label" columns={COLUMNS} rows={ROWS} getRowKey={slugOf} />,
      );

      expectUnavailable('This table has no caption to name it.');
    });

    it('renders it for a portal caption, whose content would render outside the caption', () => {
      render(
        <DataTable
          caption={createPortal(<span>Elsewhere</span>, document.body)}
          captionLabel="Elsewhere"
          columns={COLUMNS}
          rows={ROWS}
          getRowKey={slugOf}
        />,
      );

      expectUnavailable('This table has no caption to name it.');
      expect(screen.queryByText('Elsewhere')).toBeNull();
    });

    it('names both missing parts when there is no caption and no column', () => {
      render(<DataTable caption=" " columns={[]} rows={ROWS} getRowKey={slugOf} />);

      expectUnavailable('This table has no caption to name it and no columns to show.');
    });

    it.each([
      ['a blank captionLabel', '   '],
      ['an empty captionLabel', ''],
    ])('renders it for a rich caption with %s', (_kind, captionLabel) => {
      render(
        <DataTable
          caption={<span>Evidence</span>}
          captionLabel={captionLabel}
          columns={COLUMNS}
          rows={ROWS}
          getRowKey={slugOf}
        />,
      );

      expectUnavailable("This table's caption has no text label to name it.");
    });

    it('names the missing label and columns together', () => {
      render(
        <DataTable caption={<span>Evidence</span>} captionLabel="" columns={[]} rows={ROWS} getRowKey={slugOf} />,
      );

      expectUnavailable(
        "This table's caption has no text label to name it, and the table has no columns to show.",
      );
    });

    it('renders it for a named table with no columns', () => {
      renderAssets({ columns: [] });

      expectUnavailable('This table has no columns to show.');
    });

    it('recovers into a table once the configuration is fixed', () => {
      const { rerender } = render(
        <DataTable caption="" columns={COLUMNS} rows={ROWS} getRowKey={slugOf} />,
      );
      expect(screen.queryByRole('table')).toBeNull();

      rerender(<DataTable caption="Fixed" columns={COLUMNS} rows={ROWS} getRowKey={slugOf} />);

      expect(screen.getByRole('table', { name: 'Fixed' })).toBeDefined();
      expect(screen.queryByText('Table unavailable')).toBeNull();
    });
  });
});
