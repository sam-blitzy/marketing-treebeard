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
 * - an unnamed or column-less table renders the static "Table unavailable" state naming what is
 *   missing, and no table or region.
 */
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { DataTable, type DataTableColumn } from './DataTable';

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
 * The white-paper columns: an identifying row-header column, a rendered currency column, three
 * numeric default cells, a boolean and an object property, and a key no row carries.
 */
const COLUMNS: readonly DataTableColumn<AssetRow>[] = [
  { key: 'asset', header: 'Asset', isRowHeader: true },
  { key: 'spend', header: 'Spend', align: 'end', render: (row) => `€${row.spend.toFixed(2)}` },
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
      expect(cellTexts(at(bodyRows, 0))).toEqual(['Buyer guide', '€1200.00', '4', '2', '', '', '']);
      // A zero renders as 0; null and an absent property render as empty cells.
      expect(cellTexts(at(bodyRows, 1))).toEqual(['State report', '€0.00', '', '', '', '', '']);
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
      // The other row stays collapsed.
      expect(screen.getAllByRole('button', { name: 'Show details' })).toHaveLength(1);

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(toggle.textContent).toBe('Show details');
      expect(document.getElementById(detailId)?.hidden).toBe(true);
      expect(document.getElementById(detailId)?.textContent).toBe('');
    });

    it('operates the toggle from the keyboard as a native button', () => {
      renderAssets({ renderExpanded: renderDetail });
      const toggle = at(screen.getAllByRole('button', { name: 'Show details' }), 1);

      toggle.focus();
      expect(document.activeElement).toBe(toggle);
      // A native button turns Enter and Space into a click; jsdom does not synthesise that.
      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(screen.getByText('Creatives of State report')).toBeDefined();
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
