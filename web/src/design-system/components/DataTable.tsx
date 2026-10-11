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
 * - SC 1.4.1 Use of Color, SC 2.5.3 Label in Name and SC 4.1.2: an expand toggle shows its
 *   state in words, "Show details" while its row is collapsed and "Hide details" while it is
 *   expanded, and an expanded toggle is also outlined and filled. Its accessible name is that
 *   visible text followed by visually hidden text naming the row ("Show details for {row}"), so
 *   it starts with the words on screen and differs from every other row's toggle.
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
 * Sizing (SC 1.4.10 Reflow and alignment across sibling tables):
 * - Headers wrap between words, so a table wraps them only when it cannot fit its region with
 *   every column at its widest content. Numeric body cells stay on one line, except a cell's
 *   `note`, which wraps between words.
 * - An expandable table's toggle column is as narrow as its short state label, and keeps the
 *   width of the longer of its two labels, so expanding a row never shifts the other columns.
 * - A `density` of `'compact'` narrows the cells' inline padding, sets the column headers in a
 *   smaller text size and lets the toggle label wrap between its words, so a wide table of many
 *   short columns fits a narrower region before it scrolls; see {@link DataTableDensity}.
 * - A column may take a proportional `share` in twelfths of the table's inline size, rendered as
 *   a `<colgroup>`. Sibling tables of equal width that give their columns the same shares
 *   therefore share one column grid. When every column of a table that is not expandable has a
 *   fixed `width` on the `--space-4` width scale instead, the table is laid out at those fixed
 *   widths (`table-layout: fixed`), so sibling tables line up column for column whatever their
 *   content; see {@link DataTableColumnWidth}. A `minWidth` from a token-derived scale holds a
 *   column open when the table overflows, and `wrap: 'anywhere'` lets one column's body cells
 *   break unbroken tokens, such as identifiers, so the table can fit.
 * - A table wider than its region shows a scroll cue: a shadow on each edge beyond which columns
 *   are hidden, which disappears at that end of the scroll and never shows on a table that fits.
 * - A `sticky` first column stays at the region's inline start while the rest scrolls beneath
 *   it, and once the region is scrolled the cue at that edge moves to the column's inline end.
 * - The caption's content wraps within the region's visible width and stays at the region's
 *   inline start while a wider table scrolls sideways beneath it, so a caption longer than a
 *   narrow viewport is read whole at every scroll position and never widens its table.
 *
 * The styling lives in `DataTable.css`, whose class names are the contract this component
 * renders: `ds-datatable__region` and, on an expandable table's region,
 * `ds-datatable__region--expandable`, `ds-datatable` and, on a compact table,
 * `ds-datatable--compact`, and, on a table laid out at fixed column widths,
 * `ds-datatable--fixed` with one `ds-datatable__col` per column, `ds-datatable__caption` and
 * the `ds-datatable__caption-content` wrapper of its content,
 * `ds-datatable__col--share-1` to `--share-12` on the `<col>` of a column with a `share`,
 * `ds-datatable__header`, `ds-datatable__cell`, `ds-datatable__cell--start` and `--end`,
 * `ds-datatable__cell--min-sm`, `--min-md`, `--min-lg` and `--min-xl`,
 * `ds-datatable__cell--sticky`,
 * `ds-datatable__cell--wrap-anywhere`, `ds-datatable__cell-note`, `ds-datatable__toggle-cell`,
 * `ds-datatable__toggle-state`, `ds-datatable__detail-row`, `ds-datatable__detail-cell`,
 * `ds-datatable__detail-content` and `ds-datatable__empty`. The visually hidden "Details"
 * header of the expand column and each toggle's visually hidden row name use
 * `ds-visually-hidden` from `shared.css`, and the configuration state is styled by
 * `EmptyState.css`. The only inline style is the component-private `--ds-datatable-col-steps`
 * property of each fixed-width column, which `.ds-datatable__col` reads; React writes it through
 * the CSSOM, which the `default-src 'self'` CSP permits.
 *
 * The table deliberately has no sorting, pagination or selection: rows arrive already ranked by
 * the analysis run (AAP 0.5.4), and no screen in AAP 0.5.5 asks for any of the three.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled by semantic tokens, except for the
 * visually hidden utility's `1px` box and `inset(50%)` clip that `../GAPS.md` lists,
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
import type { CSSProperties, ReactNode } from 'react';

import { Button } from './Button';
import { EmptyState } from './EmptyState';

/**
 * The inline alignment of a {@link DataTable} header or cell: `'end'` for numbers, which then
 * use tabular figures and stay on one line, `'start'` for text, which wraps between words.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export type DataTableAlign = 'start' | 'end';

/**
 * A column's proportional inline size, in twelfths of the table's inline size: `3` is a quarter
 * of the table, `6` half of it.
 *
 * GAP: bind to Blitzy OS on source arrival. The twelfths are a product grid, not a Blitzy OS
 * value; on binding, they map onto the Blitzy OS column sizing.
 */
export type DataTableColumnShare = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12;

/** The steps of the column width scale, {@link DataTableColumnWidth}, from narrowest to widest. */
const COLUMN_WIDTH_STEPS = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16] as const;

/**
 * A column width on the table's width scale, in `--space-4` steps: `7` is seven `--space-4`
 * steps (7rem, 112 CSS px at the placeholder scale and a 16 px root). The scale runs from 4 to 16
 * steps, so every width is a multiple of a spacing token.
 *
 * When every column of a table has a width and the table is not expandable, the table is laid
 * out at fixed column widths (`table-layout: fixed`, the columns sized by a `<colgroup>`):
 * - the table is as wide as the larger of its region and the sum of its column widths, so a
 *   narrower region scrolls the table on the inline axis (WCAG 2.2 SC 1.4.10 Reflow) and a wider
 *   one shares the extra space out over the columns;
 * - content never widens a column, so two tables whose columns carry the same widths put each
 *   column at the same offset in regions of the same width;
 * - headers wrap between words, and a word longer than its column breaks instead of overflowing
 *   into the next one. Status chips keep their words whole and end-aligned cells stay on one
 *   line, so a column holding either is sized for its longest value.
 *
 * A table whose columns are only partly sized keeps the automatic layout and ignores the widths:
 * in a fixed layout an unsized column gets only the space the sized ones leave, which is none
 * once the region is narrower than their sum. An expandable table keeps it as well, because its
 * toggle column is sized by its button label, which no width on the scale can anticipate. A
 * fixed layout takes precedence over proportional shares ({@link DataTableColumnShare}), which
 * apply only to a table in the automatic layout.
 *
 * GAP: bind to Blitzy OS on source arrival. This is a product scale, not a Blitzy OS one; on
 * binding, it maps onto the Blitzy OS column sizing.
 */
export type DataTableColumnWidth = (typeof COLUMN_WIDTH_STEPS)[number];

/**
 * A column's minimum inline size, a step of a scale derived from the `--space-8` token, padding
 * included: `'sm'` is 2 steps (64 px at the 16 px root), `'md'` 4 (128 px), `'lg'` 6 (192 px)
 * and `'xl'` 8 (256 px). Rebinding the spacing scale rebinds these sizes.
 *
 * GAP: bind to Blitzy OS on source arrival. The scale is a product derivation, not a Blitzy OS
 * value; on binding, it maps onto the Blitzy OS column sizing.
 */
export type DataTableColumnMinWidth = 'sm' | 'md' | 'lg' | 'xl';

/**
 * How a column's body cells break text that does not fit: `'anywhere'` lets an unbroken token,
 * such as an environment variable name, a URN or a dotted key, break between any two characters.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export type DataTableColumnWrap = 'anywhere';

/**
 * The spacing density of a {@link DataTable}'s own header and body cells.
 *
 * - `'default'`: a `--space-3` step of inline padding on each side of every cell, headers in the
 *   table's `--font-size-sm`, and an expand toggle whose label stays on one line.
 * - `'compact'`, for a wide table of many short columns such as the white-paper tables: a
 *   `--space-1` step of inline padding on each side, headers in `--font-size-xs`, and an expand
 *   toggle whose label wraps between its words ("Show" above "details"), so the table fits a
 *   narrower region before it scrolls. Body text keeps the table's size.
 *
 * Both densities keep the cells' block padding, the caption, the detail and empty cells, the
 * column minimums, and the toggle cell's own padding, which clears its focus ring from the
 * region's edge and from a sticky column beside it.
 *
 * GAP: bind to Blitzy OS on source arrival. The densities are product values, not Blitzy OS
 * ones; on binding, they map onto the Blitzy OS table density.
 */
export type DataTableDensity = 'default' | 'compact';

/**
 * One column of a {@link DataTable}, typed by the row it reads.
 *
 * - Numeric columns (spend, counts, rates, costs) pass `align: 'end'`, so their header and cells
 *   align to the end of the line and use tabular figures, and their body cells stay on one line;
 *   text columns keep the default `'start'`, and their cells wrap between words. Headers always
 *   wrap between words, so a long header never sets a column's minimum width.
 * - `alignCell` aligns each body cell by its row's value, for a column whose rows mix numbers and
 *   text, such as an evidence table's Value column: numbers end-aligned, text start-aligned. It
 *   overrides `align` for body cells only; the header keeps `align`.
 * - `isRowHeader` renders the column's cells as `<th scope="row">`, so assistive technology
 *   announces each cell of the row with it. Use it on the first identifying column, for example
 *   the asset name in a white paper table or the metric label in an evidence table, and on at
 *   most one column.
 * - Without `render`, a cell shows `row[key]` when that property is a string or a number, and is
 *   empty otherwise, so `null` or `undefined` never appear as visible text. Columns that format a
 *   value (currency, percentages, floors such as "≥ 12", "unavailable") pass `render`.
 * - `width` fixes the column's width on the table's width scale. Tables that share a column set
 *   and give every column the same width line up column for column; see
 *   {@link DataTableColumnWidth} for when the widths apply and how the table is then laid out.
 * - `share` gives the column a share of the table's inline size, in twelfths. Sibling tables of
 *   equal width whose columns take the same shares line their columns up on one grid. A share is
 *   a preferred size: a column whose content cannot fit its share takes its content's minimum
 *   instead, and the browser fits the other shares into what is left, or the table overflows its
 *   region and scrolls, so no content is ever clipped. Give the columns of one table at most 12
 *   twelfths in total. An expandable table's toggle column takes no share: it keeps its button's
 *   width, and the browser fits the shares into the rest. Shares apply only to a table that keeps
 *   the automatic layout, so they are ignored on a table laid out at fixed `width`s.
 * - `minWidth` holds the column open to a step of the {@link DataTableColumnMinWidth} scale, on
 *   its header and body cells, even when the table is wider than its region and every other
 *   column shrinks to its content's minimum. Use it for a column whose text would otherwise wrap
 *   to a word per line, such as an asset name or an action.
 * - `wrap: 'anywhere'` lets the column's body cells break an unbroken token between any two
 *   characters, which lowers the column's minimum width to a character, so a table holding long
 *   identifiers can fit its region. Opt in only for identifier, key or free-text reason columns:
 *   ordinary words and status chips keep breaking only between words.
 * - `note` adds a short plain-text note after a body cell's value, in muted text, such as the
 *   reason a cost is "—": "— (no QL yet)". The value keeps its alignment and, in an end-aligned
 *   column, stays on one line; the note wraps between words, its first word on the value's line,
 *   so a long note never sets the column's minimum width beyond its longest word.
 * - `sticky` keeps the first column, typically the row headers, in view at the region's inline
 *   start while a table wider than its region scrolls sideways, so every row stays named while
 *   its far columns are read. Only the first data column takes it, after an expandable table's
 *   toggle column, which scrolls out past the region's inline start before the column sticks; on
 *   any other column it has no effect. Once the region is scrolled, the column's inline-end edge
 *   shows the scroll cue, because the columns hidden beneath it lie there (in an expandable
 *   table, from the first scrolled pixel, while the toggle column is still scrolling out).
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
  align?: DataTableAlign;
  /**
   * The inline alignment of one row's body cell, overriding `align` for that cell. Omitted, or
   * returning anything but `'start'` or `'end'`, the cell takes `align`. The header keeps `align`.
   */
  alignCell?: (row: Row) => DataTableAlign;
  /** Renders the cell content for a row. Omitted, the cell shows `row[key]` (see above). */
  render?: (row: Row) => ReactNode;
  /** Renders the cells as row headers (`<th scope="row">`). Defaults to `false`. */
  isRowHeader?: boolean;
  /**
   * The column's width on the table's width scale. It applies only when every column of the
   * table has one and the table is not expandable ({@link DataTableColumnWidth}). Omitted, the
   * column is sized from its content, as is every column of a table that keeps the automatic
   * layout.
   */
  width?: DataTableColumnWidth;
  /**
   * The column's preferred share of the table's inline size, in twelfths (an integer from 1 to
   * 12), in a table that keeps the automatic layout. Omitted, or not such an integer, the column
   * is sized by its content alone.
   */
  share?: DataTableColumnShare;
  /**
   * The column's minimum inline size on its header and body cells, a step of the
   * {@link DataTableColumnMinWidth} scale. Omitted, or not a step of it, the column's minimum is
   * its content's.
   */
  minWidth?: DataTableColumnMinWidth;
  /**
   * How the column's body cells break text: `'anywhere'` breaks unbroken tokens between any two
   * characters. Omitted, or any other value, text breaks only between words.
   */
  wrap?: DataTableColumnWrap;
  /**
   * Returns the plain-text note shown after a row's body cell value, separated by a space, for
   * example "(no QL yet)" after "—". Returning `null`, `undefined`, a blank string or anything
   * but a string shows no note. Omitted, no cell of the column has one.
   */
  note?: (row: Row) => string | null | undefined;
  /**
   * Keeps the column's header and body cells in view at the region's inline start while the
   * table scrolls sideways. Taken only by the first entry of `columns`; on any other column it
   * has no effect. Defaults to `false`.
   */
  sticky?: boolean;
}

/**
 * The props of {@link DataTable} other than its caption props: the columns, rows, row keys,
 * empty-table message, expansion and density. {@link DataTableProps} adds the caption props.
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
   * Returns the plain-text name of a row, which completes the accessible name of its toggle.
   * The toggle shows "Show details" while the row is collapsed and "Hide details" while it is
   * expanded; the row's name follows that text as visually hidden text, so the toggle is named
   * "Show details for {name}" or "Hide details for {name}" and its visible text stays short.
   * Return a name that is unique on the page, such as an asset with its currency, "The 2026 B2B
   * Pipeline Report (EUR)", so no two toggles share a name. Omitted, or returning a blank string
   * or anything but a string, the toggle is named by its visible text alone.
   */
  getRowLabel?: (row: Row) => string;
  /**
   * The spacing density of the table's own header and body cells ({@link DataTableDensity}).
   * Defaults to `'default'`, and any value but `'compact'`, which only an untyped caller can
   * pass, is `'default'`. It never reaches a table nested in the detail rows, which takes its
   * own `density`.
   */
  density?: DataTableDensity;
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
 * toggle buttons, "Show details" or "Hide details" and named for their row through
 * `getRowLabel`, and each data row is followed by a detail row spanning every column, hidden
 * until its toggle is pressed. The white paper table uses it to expand each asset row to its
 * per-creative rows, including "unknown creative" (AAP 0.5.5). Expanded rows are remembered by
 * `getRowKey`, so a row stays open when the rows are re-rendered in another order. The expanded
 * content is laid out at the region's visible width and stays in view while the row's columns
 * scroll sideways, so it may hold its own tables, each scrolling in its own region.
 *
 * `density: 'compact'` sets the table's own cells closer together ({@link DataTableDensity}).
 * The white-paper asset and per-creative tables take it, so their twelve and ten columns fit a
 * desktop region before they scroll. A table nested in a compact table's detail rows keeps its
 * own density.
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

/** The visible text of a collapsed row's toggle. */
const SHOW_DETAILS = 'Show details';

/** The visible text of an expanded row's toggle. */
const HIDE_DETAILS = 'Hide details';

/**
 * Trimmed text, or `null` for anything that is not a string with non-whitespace text. The
 * parameter is typed for what untyped callers can return from `getRowLabel` or `note`.
 *
 * @param value a row label or cell note as a caller returned it
 * @returns the trimmed text, or `null` when there is none to show
 */
function plainText(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** The largest proportional column `share`: the table's full inline size, in twelfths. */
const MAX_COLUMN_SHARE = 12;

/** The {@link DataTableColumnMinWidth} steps that `DataTable.css` defines a cell class for. */
const MIN_WIDTH_STEPS: ReadonlySet<unknown> = new Set<DataTableColumnMinWidth>([
  'sm',
  'md',
  'lg',
  'xl',
]);

/**
 * The `<col>` class of a column's proportional `share`: `ds-datatable__col--share-{n}` for an
 * integer `n` from 1 to 12. Any other value, which only an untyped caller can pass, has no class,
 * so the column is sized by its content alone.
 *
 * @param share the column's `share` prop
 * @returns the class name, or `undefined` for a column without a valid share
 */
function colClassName(share: unknown): string | undefined {
  return typeof share === 'number' &&
    Number.isInteger(share) &&
    share >= 1 &&
    share <= MAX_COLUMN_SHARE
    ? `ds-datatable__col--share-${String(share)}`
    : undefined;
}

/**
 * The class names of the `<table>`: `ds-datatable`, plus `ds-datatable--compact` for the compact
 * {@link DataTableDensity} and `ds-datatable--fixed` for a table laid out at fixed column widths.
 *
 * @param density the table's `density` prop
 * @param fixed whether the table is laid out at fixed column widths (see {@link fixedColumns})
 * @returns the space-separated class names
 */
function tableClassName(density: unknown, fixed: boolean): string {
  return [
    'ds-datatable',
    ...(density === 'compact' ? ['ds-datatable--compact'] : []),
    ...(fixed ? ['ds-datatable--fixed'] : []),
  ].join(' ');
}

/** The steps of {@link DataTableColumnWidth}, to check a width at run time. */
const COLUMN_WIDTHS: ReadonlySet<unknown> = new Set(COLUMN_WIDTH_STEPS);

/**
 * Whether a value is a step of the column width scale. The check runs on the value itself, so a
 * JavaScript caller or an untyped spread that passes a width off the scale, such as `7.5`, `40`
 * or `'7'`, is caught as well.
 *
 * @param value the `width` of a column
 * @returns `true` when the value is one of {@link COLUMN_WIDTH_STEPS}
 */
function isColumnWidth(value: unknown): value is DataTableColumnWidth {
  return COLUMN_WIDTHS.has(value);
}

/** One `<col>` of a table laid out at fixed column widths. */
interface FixedColumn {
  /** The column's key: the React key of its `<col>`. */
  readonly key: string;
  /** The column's width on the scale. */
  readonly width: DataTableColumnWidth;
}

/**
 * The columns of a table laid out at fixed widths, in display order, or `null` when the table
 * keeps the automatic layout: when it has no columns, is expandable, or has a column without a
 * width on the scale (see {@link DataTableColumnWidth} for why a partly sized table is not fixed).
 *
 * @param columns the table's columns
 * @param expandable whether the table has a toggle column (`renderExpanded` is given)
 * @returns one entry per column, or `null` for the automatic layout
 */
function fixedColumns<Row>(
  columns: readonly DataTableColumn<Row>[],
  expandable: boolean,
): readonly FixedColumn[] | null {
  if (expandable || columns.length === 0) {
    return null;
  }
  const fixed: FixedColumn[] = [];
  for (const column of columns) {
    const width: unknown = column.width;
    if (!isColumnWidth(width)) {
      return null;
    }
    fixed.push({ key: column.key, width });
  }
  return fixed;
}

/**
 * Inline style of a fixed-width `<col>`. It holds only the component-private custom property
 * that `.ds-datatable__col` reads, typed without a cast and still assignable to `CSSProperties`.
 */
type ColumnStyle = CSSProperties & Record<'--ds-datatable-col-steps', string>;

/**
 * The classes a column's header and body cells share beyond their alignment: its minimum-width
 * step (`ds-datatable__cell--min-{step}`), when it is one of {@link MIN_WIDTH_STEPS}, and the
 * sticky class (`ds-datatable__cell--sticky`) when it is the table's sticky column.
 *
 * @param column the column
 * @param sticky whether the column is the table's sticky column (see {@link stickyColumnKey})
 * @returns the class names, possibly none
 */
function sizeClassNames<Row>(column: DataTableColumn<Row>, sticky: boolean): string[] {
  return [
    ...(MIN_WIDTH_STEPS.has(column.minWidth)
      ? [`ds-datatable__cell--min-${String(column.minWidth)}`]
      : []),
    ...(sticky ? ['ds-datatable__cell--sticky'] : []),
  ];
}

/**
 * The key of the table's sticky column: the first column's, when it has `sticky: true`.
 * `sticky` on any later column is ignored, because a second column stuck at the same edge would
 * cover the first.
 *
 * @param columns the table's columns, in display order
 * @returns the sticky column's key, or `null` when no column sticks
 */
function stickyColumnKey<Row>(columns: readonly DataTableColumn<Row>[]): string | null {
  const [first] = columns;
  return first?.sticky === true ? first.key : null;
}

/**
 * The class names of a column's header cell: the header class, the column's `align` and its
 * size classes. `alignCell` never applies here, so a mixed column keeps one header alignment.
 *
 * @param column the column
 * @param sticky whether the column is the table's sticky column
 * @returns the space-separated class names
 */
function headerClassName<Row>(column: DataTableColumn<Row>, sticky: boolean): string {
  return [
    'ds-datatable__header',
    `ds-datatable__cell--${column.align ?? 'start'}`,
    ...sizeClassNames(column, sticky),
  ].join(' ');
}

/**
 * The class names of a column's body cell in one row: the cell class, the row's alignment (the
 * column's `alignCell` result when it is `'start'` or `'end'`, otherwise the column's `align`),
 * the column's size classes and, for `wrap: 'anywhere'`, the break-anywhere class.
 *
 * @param column the column
 * @param row the row the cell renders
 * @param sticky whether the column is the table's sticky column
 * @returns the space-separated class names
 */
function cellClassName<Row>(column: DataTableColumn<Row>, row: Row, sticky: boolean): string {
  const own: unknown = column.alignCell?.(row);
  const align = own === 'start' || own === 'end' ? own : (column.align ?? 'start');
  return [
    'ds-datatable__cell',
    `ds-datatable__cell--${align}`,
    ...sizeClassNames(column, sticky),
    ...(column.wrap === 'anywhere' ? ['ds-datatable__cell--wrap-anywhere'] : []),
  ].join(' ');
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
 * row. Its visible text, in a `ds-datatable__toggle-state` span, is "Show details" while the row
 * is collapsed and "Hide details" while it is expanded; the span's `data-alternate` attribute
 * holds the other text, which `DataTable.css` lays out invisibly beneath it, so the button keeps
 * the width of the longer text in both states. When `getRowLabel` returns a name, a space and
 * then a `ds-visually-hidden` span reading "for {name}" follow it, which complete the button's
 * accessible name; the space sits between the spans, so every accessible-name computation
 * separates the words, and as the line's trailing space it takes no visible width. The detail
 * row is always in the DOM, so `aria-controls` always resolves; it carries `hidden` while
 * collapsed, and its content is rendered only while expanded. Detail row ids are derived from
 * `useId` and the row index, so any row key is safe and ids never collide between two tables on
 * one page.
 *
 * The caption and the detail cell are as wide as the whole table, which can be far wider than
 * the region's visible area. The caption's content therefore sits in a
 * `ds-datatable__caption-content` wrapper, a `<div>` because a rich caption may hold block
 * content, and `DataTable.css` lays it out at most at the region's visible inline size and pins
 * it at the region's inline start, so the whole caption stays in view while the table scrolls
 * sideways. The `<caption>` itself, with its id, stays the table's first child, so the table and
 * the region are named exactly as above. An expandable table's region also carries
 * `ds-datatable__region--expandable`, and the expanded content sits in a
 * `ds-datatable__detail-content` wrapper laid out and pinned the same way, so the whole of it
 * stays in view while the row's columns scroll sideways; a nested table wider than that scrolls
 * in its own region. A table without `renderExpanded` renders neither class.
 *
 * When every column has a `width` and the table is not expandable, the table carries
 * `ds-datatable--fixed` and a `<colgroup>` after its caption, with one `ds-datatable__col` per
 * column whose `--ds-datatable-col-steps` property holds the column's width in `--space-4` steps;
 * `DataTable.css` lays the table out at those widths ({@link DataTableColumnWidth}). Otherwise,
 * when at least one column has a valid `share`, a `<colgroup>` follows the caption with one
 * `<col>` per rendered column, in order: an expandable table's toggle column first, with no
 * class, then each data column, carrying `ds-datatable__col--share-{n}` when it has a share. A
 * table with neither renders no `<colgroup>`. A column's `minWidth` adds
 * `ds-datatable__cell--min-{step}` to its header and body cells, its `wrap: 'anywhere'` adds
 * `ds-datatable__cell--wrap-anywhere` to its body cells, and its `alignCell` picks each body
 * cell's `--start` or `--end` class. A row's `note`, when it has text, follows the cell's
 * content after a space, in a `ds-datatable__cell-note` span. When the first column has
 * `sticky: true`, its header and body cells carry `ds-datatable__cell--sticky`. A `density` of
 * `'compact'` adds `ds-datatable--compact` to the `<table>`, and no class to the region or any
 * cell; any other `density` adds nothing.
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
 *   {
 *     key: 'costPerQl',
 *     header: 'Cost per Qualified Lead',
 *     align: 'end',
 *     render: (r) => r.costPerQlText,
 *     note: (r) => r.costPerQlNote, // "(no QL yet)" after "—"
 *   },
 * ];
 *
 * // Each toggle reads "Show details" or "Hide details", and is named
 * // "Show details for {asset} (EUR)" or "Hide details for {asset} (EUR)".
 * // Compact density sets the many short columns closer together.
 * <DataTable
 *   caption="White papers · EUR"
 *   columns={columns}
 *   rows={rows}
 *   getRowKey={(r) => r.slug}
 *   emptyMessage="No white papers in this window"
 *   renderExpanded={(r) => <CreativeRows asset={r} />}
 *   getRowLabel={(r) => `${r.asset} (EUR)`}
 *   density="compact"
 * />
 *
 * <DataTable
 *   caption={<>Evidence · <abbr title="Euro">EUR</abbr></>}
 *   captionLabel="Evidence · EUR"
 *   columns={evidenceColumns}
 *   rows={evidenceRows}
 *   getRowKey={(r) => r.metric}
 * />
 *
 * // Sibling tables sharing one grid; a mixed Value column; an identifier column that may break.
 * const gridColumns: readonly DataTableColumn<MetricRow>[] = [
 *   { key: 'metric', header: 'Metric', isRowHeader: true, share: 5, minWidth: 'sm' },
 *   {
 *     key: 'value',
 *     header: 'Value',
 *     align: 'end',
 *     share: 3,
 *     alignCell: (r) => (typeof r.value === 'number' ? 'end' : 'start'),
 *   },
 *   { key: 'property', header: 'Property', share: 4, wrap: 'anywhere' },
 * ];
 *
 * // Sibling tables at fixed widths on the --space-4 scale, aligned whatever their content.
 * const freshnessColumns: readonly DataTableColumn<FreshnessRow>[] = [
 *   { key: 'dataset', header: 'Dataset', isRowHeader: true, width: 13 },
 *   { key: 'reason', header: 'Reason', width: 12 },
 * ];
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
  getRowLabel,
  density,
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
  const fixed = fixedColumns(columns, expandable);
  // A share grid is rendered only in the automatic layout, and only when some column takes a
  // share, so other tables keep theirs.
  const hasShareGrid =
    fixed === null && columns.some((column) => colClassName(column.share) !== undefined);
  const stickyKey = stickyColumnKey(columns);

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
      <table className={tableClassName(density, fixed !== null)} aria-label={label}>
        <caption id={captionId} className="ds-datatable__caption">
          <div className="ds-datatable__caption-content">{caption}</div>
        </caption>
        {fixed === null ? null : (
          <colgroup>
            {fixed.map((column) => {
              const style: ColumnStyle = { '--ds-datatable-col-steps': String(column.width) };
              return <col key={column.key} className="ds-datatable__col" style={style} />;
            })}
          </colgroup>
        )}
        {hasShareGrid ? (
          <colgroup>
            {expandable ? <col /> : null}
            {columns.map((column) => (
              <col key={column.key} className={colClassName(column.share)} />
            ))}
          </colgroup>
        ) : null}
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
                className={headerClassName(column, column.key === stickyKey)}
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
              const rowLabel = expandable ? plainText(getRowLabel?.(row)) : null;
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
                          <span
                            className="ds-datatable__toggle-state"
                            data-alternate={expanded ? SHOW_DETAILS : HIDE_DETAILS}
                          >
                            {expanded ? HIDE_DETAILS : SHOW_DETAILS}
                          </span>
                          {rowLabel === null ? null : (
                            <>
                              {' '}
                              <span className="ds-visually-hidden">{`for ${rowLabel}`}</span>
                            </>
                          )}
                        </Button>
                      </td>
                    ) : null}
                    {columns.map((column) => {
                      const className = cellClassName(column, row, column.key === stickyKey);
                      const content = column.render
                        ? column.render(row)
                        : defaultCell(row, column.key);
                      const note = plainText(column.note?.(row));
                      const body =
                        note === null ? (
                          content
                        ) : (
                          <>
                            {content}{' '}
                            <span className="ds-datatable__cell-note">{note}</span>
                          </>
                        );
                      return column.isRowHeader === true ? (
                        <th key={column.key} scope="row" className={className}>
                          {body}
                        </th>
                      ) : (
                        <td key={column.key} className={className}>
                          {body}
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
