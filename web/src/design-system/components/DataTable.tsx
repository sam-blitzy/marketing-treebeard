/**
 * `DataTable`: the product's typed, captioned data table, with numeric alignment and optional
 * expandable detail rows (AAP 0.4.2, status GAP).
 *
 * It renders the evidence tables on recommendation cards, the copy variants tables and the white
 * paper tables (AAP 0.5.5). Feature code never renders raw table elements (AAP 0.4.4): it passes
 * typed `columns` and `rows`, and this component owns the markup. Each column is defined by the
 * feature, so the table never merges values; for example MQL and Qualified Lead stay two separate
 * columns because the feature declares two columns.
 *
 * Accessibility (WCAG 2.2 AA, AAP 0.1.1):
 * - SC 1.3.1 Info and Relationships: a native `<table>` with a `<caption>` (every table has one,
 *   AAP 0.5.5), column headers as `<th scope="col">` and, for the identifying column, row headers
 *   as `<th scope="row">`.
 * - SC 1.4.10 Reflow: the table sits in a region that scrolls on the inline axis, so a wide table
 *   never makes the page scroll sideways at a 320 CSS px viewport.
 * - SC 2.1.1 Keyboard: that region is focusable (`tabindex="0"`), so keyboard users can scroll it
 *   with the arrow keys, and it is named by the caption so its focus stop is announced. The
 *   expand toggles are native buttons carrying `aria-expanded` and `aria-controls`.
 *
 * The styling lives in `DataTable.css`, whose class names are the contract this component
 * renders: `ds-datatable__region`, `ds-datatable`, `ds-datatable__caption`,
 * `ds-datatable__header`, `ds-datatable__cell`, `ds-datatable__cell--start` and `--end`,
 * `ds-datatable__toggle-cell`, `ds-datatable__detail-row`, `ds-datatable__detail-cell` and
 * `ds-datatable__empty`. The visually hidden "Details" header of the expand column uses
 * `ds-visually-hidden` from `shared.css`.
 *
 * The table deliberately has no sorting, pagination or selection: rows arrive already ranked by
 * the analysis run (AAP 0.5.4), and no screen in AAP 0.5.5 asks for any of the three.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `DataTable.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './shared.css';
import './DataTable.css';

import { Fragment, useId, useState } from 'react';
import type { ReactNode } from 'react';

import { Button } from './Button';

/**
 * One column of a {@link DataTable}, typed by the row it reads.
 *
 * - Numeric columns (spend, counts, rates, costs) pass `align: 'end'`, so their header and cells
 *   align to the end of the line and use tabular figures; text columns keep the default
 *   `'start'`.
 * - `isRowHeader` renders the column's cells as `<th scope="row">`, so assistive technology
 *   announces each cell of the row with it. Use it on the first identifying column, for example
 *   the asset name in a white paper table or the metric label in an evidence table, and on at
 *   most one column.
 * - Without `render`, a cell shows `row[key]` when that property is a string or a number, and is
 *   empty otherwise, so `null` or `undefined` never appear as visible text. Columns that format a
 *   value (currency, percentages, floors such as "≥ 12", "unavailable") pass `render`.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level column props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS table column definition.
 */
export interface DataTableColumn<Row> {
  /** Unique within the table; the React key of the column and the default cell's property. */
  key: string;
  /** The column header content. */
  header: ReactNode;
  /** The inline alignment of the header and cells. Defaults to `'start'`. */
  align?: 'start' | 'end';
  /** Renders the cell content for a row. Omitted, the cell shows `row[key]` (see above). */
  render?: (row: Row) => ReactNode;
  /** Renders the cells as row headers (`<th scope="row">`). Defaults to `false`. */
  isRowHeader?: boolean;
}

/**
 * Props of {@link DataTable}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because feature
 * code must not restyle a wrapper (AAP 0.4.4).
 *
 * Expansion is enabled by passing `renderExpanded`. The table then adds a leading column of
 * toggle buttons, and each data row is followed by a detail row spanning every column, hidden
 * until its toggle is pressed. The white paper table uses it to expand each asset row to its
 * per-creative rows, including "unknown creative" (AAP 0.5.5). Expanded rows are remembered by
 * `getRowKey`, so a row stays open when the rows are re-rendered in another order.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS props;
 * on binding, they map onto the Blitzy OS table component.
 */
export interface DataTableProps<Row> {
  /** The columns, in display order. */
  columns: readonly DataTableColumn<Row>[];
  /** The rows, in display order. Zero rows render `emptyMessage` in a single full-width cell. */
  rows: readonly Row[];
  /** Returns a stable key, unique within `rows`, for a row: the React key and expansion id. */
  getRowKey: (row: Row) => string;
  /**
   * The table's caption, for example "White papers · EUR". Required, because every table has
   * one (AAP 0.5.5); it names both the table and its scrolling region.
   */
  caption: ReactNode;
  /** Shown when `rows` is empty. Defaults to "No rows". */
  emptyMessage?: ReactNode;
  /** Renders the detail content of an expanded row. Omitted, the table is not expandable. */
  renderExpanded?: (row: Row) => ReactNode;
  /**
   * The accessible label of a row's toggle button. Defaults to "Show details" and "Hide
   * details". Pass a label naming the row, such as "Show creatives for {asset}", so every toggle
   * in the table has a distinct name. The label is also the button's visible text and does not
   * wrap, so it sets the width of the toggle column: keep it short.
   */
  expandLabel?: (row: Row, expanded: boolean) => string;
}

/** The empty-table message used when `emptyMessage` is not given. */
const DEFAULT_EMPTY_MESSAGE = 'No rows';

/**
 * The default content of a cell whose column has no `render`: the row's `key` property when it
 * is a string or a number, otherwise nothing. Reading through `Reflect.get` after an `in` check
 * keeps the lookup type-safe for any row type without a cast, and anything else (objects,
 * booleans, `null`, `undefined`) renders as an empty cell rather than as visible text.
 *
 * @param row the row being rendered
 * @param key the column key, read as the row's property name
 * @returns the cell content, or `null` for an empty cell
 */
function defaultCell(row: unknown, key: string): ReactNode {
  if (typeof row === 'object' && row !== null && key in row) {
    const value: unknown = Reflect.get(row, key);
    if (typeof value === 'string' || typeof value === 'number') {
      return value;
    }
  }
  return null;
}

/**
 * Data table: renders a focusable, caption-named scrolling region holding one native `<table>`
 * with its caption, a header row of `<th scope="col">` cells and one body row per entry of
 * `rows`, or a single empty-state row when there are none.
 *
 * When `renderExpanded` is given, the first column holds a quiet, small {@link Button} per row
 * whose `aria-expanded` reflects the row's state and whose `aria-controls` names the row's detail
 * row. The detail row is always in the DOM, so `aria-controls` always resolves; it carries
 * `hidden` while collapsed, and its content is rendered only while expanded. Detail row ids are
 * derived from `useId` and the row index, so any row key is safe and ids never collide between
 * two tables on one page.
 *
 * The only state is the set of expanded row keys; the component has no effects.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * const columns: readonly DataTableColumn<AssetRow>[] = [
 *   { key: 'asset', header: 'Asset', isRowHeader: true },
 *   { key: 'spend', header: 'Spend', align: 'end', render: (r) => formatMoney(r.spend) },
 *   { key: 'mql', header: 'MQL (First meeting booked)', align: 'end' },
 *   { key: 'ql', header: 'Qualified Lead', align: 'end' },
 * ];
 *
 * <DataTable
 *   caption="White papers · EUR"
 *   columns={columns}
 *   rows={rows}
 *   getRowKey={(r) => r.slug}
 *   emptyMessage="No white papers in this window"
 *   renderExpanded={(r) => <CreativeRows asset={r} />}
 *   expandLabel={(r, open) => `${open ? 'Hide' : 'Show'} creatives for ${r.asset}`}
 * />
 * ```
 */
export function DataTable<Row>({
  columns,
  rows,
  getRowKey,
  caption,
  emptyMessage,
  renderExpanded,
  expandLabel,
}: DataTableProps<Row>) {
  const baseId = useId();
  const captionId = `${baseId}-caption`;
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(() => new Set());

  const expandable = renderExpanded !== undefined;
  const totalColumns = columns.length + (expandable ? 1 : 0);

  /** Opens a collapsed row or closes an open one, replacing the set so React sees a change. */
  const toggle = (key: string): void => {
    setExpandedKeys((previous) => {
      const next = new Set(previous);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  return (
    <div className="ds-datatable__region" role="region" aria-labelledby={captionId} tabIndex={0}>
      <table className="ds-datatable">
        <caption id={captionId} className="ds-datatable__caption">
          {caption}
        </caption>
        <thead>
          <tr>
            {expandable ? (
              <th scope="col" className="ds-datatable__header ds-datatable__toggle-cell">
                <span className="ds-visually-hidden">Details</span>
              </th>
            ) : null}
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`ds-datatable__header ds-datatable__cell--${column.align ?? 'start'}`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td className="ds-datatable__empty" colSpan={totalColumns}>
                {emptyMessage ?? DEFAULT_EMPTY_MESSAGE}
              </td>
            </tr>
          ) : (
            rows.map((row, index) => {
              const key = getRowKey(row);
              const expanded = expandedKeys.has(key);
              const detailId = `${baseId}-detail-${String(index)}`;
              return (
                <Fragment key={key}>
                  <tr>
                    {expandable ? (
                      <td className="ds-datatable__cell ds-datatable__toggle-cell">
                        <Button
                          variant="quiet"
                          size="sm"
                          aria-expanded={expanded}
                          aria-controls={detailId}
                          onClick={() => {
                            toggle(key);
                          }}
                        >
                          {expandLabel
                            ? expandLabel(row, expanded)
                            : expanded
                              ? 'Hide details'
                              : 'Show details'}
                        </Button>
                      </td>
                    ) : null}
                    {columns.map((column) => {
                      const align = column.align ?? 'start';
                      const className = `ds-datatable__cell ds-datatable__cell--${align}`;
                      const content = column.render
                        ? column.render(row)
                        : defaultCell(row, column.key);
                      return column.isRowHeader === true ? (
                        <th key={column.key} scope="row" className={className}>
                          {content}
                        </th>
                      ) : (
                        <td key={column.key} className={className}>
                          {content}
                        </td>
                      );
                    })}
                  </tr>
                  {expandable ? (
                    <tr id={detailId} className="ds-datatable__detail-row" hidden={!expanded}>
                      <td className="ds-datatable__detail-cell" colSpan={totalColumns}>
                        {expanded ? renderExpanded(row) : null}
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
