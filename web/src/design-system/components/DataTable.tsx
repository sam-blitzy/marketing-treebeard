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
 *   with the arrow keys, and it carries the table's name so its focus stop is announced. The
 *   expand toggles are native buttons carrying `aria-expanded` and `aria-controls`.
 * - SC 4.1.2 Name, Role, Value and SC 2.5.3 Label in Name: the table and its region always share
 *   one non-empty name. A text caption (a string, number or bigint) is that name: the table is
 *   named by its `<caption>` and the region by `aria-labelledby` pointing at it. A rich caption
 *   (an element or an iterable of nodes) is shown in the `<caption>` too, but what it renders is
 *   not known before rendering: it may be empty, whitespace or hidden from assistive technology.
 *   It therefore comes with a required plain-text `captionLabel`, which names both the table and
 *   the region through `aria-label` and should contain the caption's visible words.
 *
 * A table is rendered only when it can be named and has columns to lay out. A caption with no
 * content (an empty or whitespace-only string, or an iterable none of whose entries has any) or a
 * rich caption without a non-blank `captionLabel` would leave both the table and its region
 * without an accessible name, and an empty `columns` array would give the empty-state cell
 * `colSpan={0}` and every row an empty `<tr>`. In each case the component renders an
 * {@link EmptyState} titled "Table unavailable", whose reason names what is missing, instead of
 * the table. It is static content, with no live-region role.
 *
 * The styling lives in `DataTable.css`, whose class names are the contract this component
 * renders: `ds-datatable__region` and, on an expandable table's region,
 * `ds-datatable__region--expandable`, `ds-datatable`, `ds-datatable__caption`,
 * `ds-datatable__header`, `ds-datatable__cell`, `ds-datatable__cell--start` and `--end`,
 * `ds-datatable__toggle-cell`, `ds-datatable__detail-row`, `ds-datatable__detail-cell`,
 * `ds-datatable__detail-content` and `ds-datatable__empty`. The visually hidden "Details"
 * header of the expand column uses `ds-visually-hidden` from `shared.css`, and the
 * configuration state is styled by `EmptyState.css`.
 *
 * The table deliberately has no sorting, pagination or selection: rows arrive already ranked by
 * the analysis run (AAP 0.5.4), and no screen in AAP 0.5.5 asks for any of the three.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled by semantic tokens, except for the
 * "Details" header's visually hidden `1px` box and `inset(50%)` clip that `../GAPS.md` lists,
 * and binding Blitzy OS later changes only this file and `DataTable.css` (AAP 0.4.5,
 * `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './shared.css';
import './DataTable.css';

import { Fragment, isValidElement, useId, useState } from 'react';
import type { ReactNode } from 'react';

import { Button } from './Button';
import { EmptyState } from './EmptyState';

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
 * The props of {@link DataTable} other than its caption props: the columns, rows, row keys,
 * empty-table message and expansion. {@link DataTableProps} adds the caption props.
 */
interface DataTableBaseProps<Row> {
  /**
   * The columns, in display order. A table needs at least one: with none, the component
   * renders its "Table unavailable" configuration state instead of a table.
   */
  columns: readonly DataTableColumn<Row>[];
  /** The rows, in display order. Zero rows render `emptyMessage` in a single full-width cell. */
  rows: readonly Row[];
  /** Returns a stable key, unique within `rows`, for a row: the React key and expansion id. */
  getRowKey: (row: Row) => string;
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

/** A text caption of a {@link DataTable}: text or a number, which is also the table's name. */
type DataTableTextCaption = string | number | bigint;

/**
 * A rich caption of a {@link DataTable}: an element, a fragment or an iterable of nodes, such as
 * `<span>White papers · <abbr title="Euro">EUR</abbr></span>`.
 *
 * Text captions are excluded, and so are `null`, `undefined`, booleans and promises, because
 * they render nothing (or nothing yet). The intersection with `object` keeps a string, which is
 * itself an iterable of strings, from passing as a rich caption.
 */
type DataTableRichCaption = Exclude<
  ReactNode,
  DataTableTextCaption | null | undefined | boolean | PromiseLike<unknown>
> &
  object;

/** The caption props of a table whose caption is text: the caption names the table. */
interface DataTableTextCaptionProps {
  /**
   * The table's caption, for example "White papers · EUR". Required, because every table has
   * one (AAP 0.5.5). It is shown in the `<caption>`, which names the table, and the scrolling
   * region is named by it through `aria-labelledby`. A blank string, such as `''` or `'   '`,
   * renders the "Table unavailable" configuration state instead of a table.
   */
  caption: DataTableTextCaption;
  /** Not taken: a text caption is already the table's plain-text name. */
  captionLabel?: never;
}

/** The caption props of a table whose caption is rich content: `captionLabel` names the table. */
interface DataTableRichCaptionProps {
  /**
   * The table's caption as rich content, shown in the `<caption>`. Required, because every
   * table has one (AAP 0.5.5).
   *
   * What rich content renders is not known before rendering: an empty fragment, an element
   * holding only whitespace, a component that returns `null` or an `aria-hidden` element all
   * name nothing. The table is therefore named by `captionLabel`, never by this content. A
   * caption with no content at all, such as `[]` or an array of blank strings, renders the
   * "Table unavailable" configuration state instead of a table.
   */
  caption: DataTableRichCaption;
  /**
   * The plain-text accessible name of the table and its scrolling region, set as the
   * `aria-label` of both. Required with a rich caption, and must have non-whitespace text: a
   * blank label renders the "Table unavailable" configuration state instead of a table.
   *
   * It should contain the caption's visible words, in their order, for example "White papers ·
   * EUR" for `<span>White papers · <abbr title="Euro">EUR</abbr></span>`, so a speech-input
   * user can name the table by what they see (WCAG 2.5.3 Label in Name).
   */
  captionLabel: string;
}

/**
 * Props of {@link DataTable}.
 *
 * The props are deliberately closed: there is no `className` or `style` prop, because feature
 * code must not restyle a wrapper (AAP 0.4.4).
 *
 * The caption props take one of two shapes. A text caption (a string, number or bigint) names
 * the table itself and takes no `captionLabel`. A rich caption (an element, fragment or iterable
 * of nodes) requires a plain-text `captionLabel`, which names the table and its region.
 *
 * Expansion is enabled by passing `renderExpanded`. The table then adds a leading column of
 * toggle buttons, and each data row is followed by a detail row spanning every column, hidden
 * until its toggle is pressed. The white paper table uses it to expand each asset row to its
 * per-creative rows, including "unknown creative" (AAP 0.5.5). Expanded rows are remembered by
 * `getRowKey`, so a row stays open when the rows are re-rendered in another order. The expanded
 * content is laid out at the region's visible width and stays in view while the row's columns
 * scroll sideways, so it may hold its own tables, each scrolling in its own region.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS props;
 * on binding, they map onto the Blitzy OS table component.
 */
export type DataTableProps<Row> = DataTableBaseProps<Row> &
  (DataTableTextCaptionProps | DataTableRichCaptionProps);

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

/** The title of the configuration state rendered in place of a table that cannot be shown. */
const UNAVAILABLE_TITLE = 'Table unavailable';

/**
 * Whether a caption has structural content to show, judged from the node itself without
 * rendering it.
 *
 * A string with at least one non-whitespace character, a number or bigint, and a React element
 * have content. An iterable has content when at least one of its entries does, checked
 * recursively and stopping at the first such entry. `null`, `undefined`, booleans, empty or
 * whitespace-only strings and iterables without such an entry have none, and neither has
 * anything else React accepts as a node, such as a portal, whose content renders outside the
 * caption, or a promise, whose content is not known yet.
 *
 * For text this settles the table's name. For rich content it does not: an element counts as
 * content whatever it renders, so an empty fragment, an element holding only whitespace, a
 * component returning `null` and an `aria-hidden` element all pass. That is why a rich caption
 * is named by its required `captionLabel` (see {@link captionNaming}), and this check only
 * rejects rich captions that cannot show anything at all.
 *
 * Iterables are read entry by entry, so an iterable caption is an array or another re-iterable
 * collection; a one-shot iterator such as a generator, which React does not support as children
 * either, would be consumed by this check.
 *
 * @param node the caption, or one entry of an iterable caption
 * @returns `true` when the caption has content to show in the `<caption>`
 */
function hasCaptionContent(node: ReactNode): boolean {
  if (typeof node === 'string') {
    return node.trim() !== '';
  }
  if (typeof node === 'number' || typeof node === 'bigint') {
    return true;
  }
  if (node === null || node === undefined || typeof node === 'boolean') {
    return false;
  }
  if (isValidElement(node)) {
    return true;
  }
  if (Symbol.iterator in node) {
    for (const entry of node) {
      if (hasCaptionContent(entry)) {
        return true;
      }
    }
  }
  return false;
}

/**
 * How a table and its region are named, or what the caption props lack to name them:
 * - `caption`: a text caption names the table, and the region points at it;
 * - `label`: a rich caption's `captionLabel` names both through `aria-label`;
 * - `no-caption`: the caption has no content;
 * - `no-label`: a rich caption has no `captionLabel` with non-whitespace text.
 */
type CaptionNaming =
  | { readonly kind: 'caption' }
  | { readonly kind: 'label'; readonly label: string }
  | { readonly kind: 'no-caption' }
  | { readonly kind: 'no-label' };

/**
 * Decides how a table is named from its caption props, deterministically and without inspecting
 * the rendered DOM.
 *
 * A caption without content (see {@link hasCaptionContent}) names nothing. A text caption (a
 * string, number or bigint) with content is the name, and any `captionLabel` beside it is not
 * used. Any other caption is rich, and is named by `captionLabel` only when that is a string with
 * non-whitespace text. The parameters are typed for what JavaScript callers and untyped spreads
 * can pass, not only for what {@link DataTableProps} allows.
 *
 * @param caption the `caption` prop
 * @param captionLabel the `captionLabel` prop
 * @returns how the table is named, or which part of the caption props is missing
 */
function captionNaming(caption: ReactNode, captionLabel: unknown): CaptionNaming {
  if (!hasCaptionContent(caption)) {
    return { kind: 'no-caption' };
  }
  if (typeof caption === 'string' || typeof caption === 'number' || typeof caption === 'bigint') {
    return { kind: 'caption' };
  }
  if (typeof captionLabel === 'string' && captionLabel.trim() !== '') {
    return { kind: 'label', label: captionLabel };
  }
  return { kind: 'no-label' };
}

/**
 * The reason a table cannot be rendered, naming what its props lack.
 *
 * @param naming how the caption props name the table (see {@link captionNaming})
 * @param hasColumns whether `columns` holds at least one column
 * @returns the reason sentence, or `null` when the table can be rendered
 */
function unavailableReason(naming: CaptionNaming, hasColumns: boolean): string | null {
  if (naming.kind === 'no-caption') {
    return hasColumns
      ? 'This table has no caption to name it.'
      : 'This table has no caption to name it and no columns to show.';
  }
  if (naming.kind === 'no-label') {
    return hasColumns
      ? "This table's caption has no text label to name it."
      : "This table's caption has no text label to name it, and the table has no columns to show.";
  }
  if (!hasColumns) {
    return 'This table has no columns to show.';
  }
  return null;
}

/**
 * Data table: renders a focusable, named scrolling region holding one native `<table>` with its
 * caption, a header row of `<th scope="col">` cells and one body row per entry of `rows`, or a
 * single empty-state row when there are none.
 *
 * The table and the region share one name. A text caption names the table through its
 * `<caption>`, and the region through `aria-labelledby` pointing at that caption. A rich caption
 * is shown in the `<caption>` as given, and its required `captionLabel` names the table and the
 * region through `aria-label`, because what rich content renders cannot be known before it is
 * rendered.
 *
 * When the caption has no content, a rich caption has no `captionLabel` with non-whitespace
 * text, or `columns` is empty, it renders no table and no region: it renders an
 * {@link EmptyState} titled "Table unavailable" whose reason names what is missing (the caption,
 * the caption's text label, the columns, or the caption part and the columns together). A
 * nameless table would break SC 1.3.1 and leave the focusable region unnamed, and a table
 * without columns has no cells to lay out. The state is static, with no `alert` role, because
 * it reports how the table was configured rather than a change at run time.
 *
 * When `renderExpanded` is given, the first column holds a quiet, small {@link Button} per row
 * whose `aria-expanded` reflects the row's state and whose `aria-controls` names the row's detail
 * row. The detail row is always in the DOM, so `aria-controls` always resolves; it carries
 * `hidden` while collapsed, and its content is rendered only while expanded. Detail row ids are
 * derived from `useId` and the row index, so any row key is safe and ids never collide between
 * two tables on one page.
 *
 * The detail cell spans every column, so it is as wide as the whole table, which can be far
 * wider than the region's visible area. An expandable table's region therefore carries
 * `ds-datatable__region--expandable`, and the expanded content sits in a
 * `ds-datatable__detail-content` wrapper that `DataTable.css` lays out at the region's visible
 * inline size and pins at its inline start, so the whole of it stays in view while the row's
 * columns scroll sideways; a nested table wider than that scrolls in its own region. A table
 * without `renderExpanded` renders neither class.
 *
 * The only state is the set of expanded row keys; the component has no effects.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * const columns: readonly DataTableColumn<AssetRow>[] = [
 *   { key: 'asset', header: 'Asset', isRowHeader: true },
 *   {
 *     key: 'spend',
 *     header: 'Spend',
 *     align: 'end',
 *     render: (r) => formatMoney(r.spend, r.currency),
 *   },
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
 *
 * <DataTable
 *   caption={<>Evidence · <abbr title="Euro">EUR</abbr></>}
 *   captionLabel="Evidence · EUR"
 *   columns={evidenceColumns}
 *   rows={evidenceRows}
 *   getRowKey={(r) => r.metric}
 * />
 * ```
 */
export function DataTable<Row>({
  columns,
  rows,
  getRowKey,
  caption,
  captionLabel,
  emptyMessage,
  renderExpanded,
  expandLabel,
}: DataTableProps<Row>) {
  const baseId = useId();
  const captionId = `${baseId}-caption`;
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(() => new Set());

  const naming = captionNaming(caption, captionLabel);
  const reason = unavailableReason(naming, columns.length > 0);
  if (reason !== null) {
    return <EmptyState title={UNAVAILABLE_TITLE}>{reason}</EmptyState>;
  }

  // A rich caption's label names the table and the region; a text caption names them itself.
  const label = naming.kind === 'label' ? naming.label : undefined;
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
    <div
      className={
        expandable ? 'ds-datatable__region ds-datatable__region--expandable' : 'ds-datatable__region'
      }
      role="region"
      aria-label={label}
      aria-labelledby={label === undefined ? captionId : undefined}
      tabIndex={0}
    >
      <table className="ds-datatable" aria-label={label}>
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
                        {expanded ? (
                          <div className="ds-datatable__detail-content">{renderExpanded(row)}</div>
                        ) : null}
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
