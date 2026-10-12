/**
 * `SettingsPage`: the read-only `/settings` screen (AAP 0.5.5, Other screens).
 *
 * It shows the configuration the API loaded, exactly as `GET /api/v1/config` returns it
 * ({@link ConfigView}), in five sections:
 *
 * 1. **Analysis window**: `window.default_days`, `window.trusted_min_days`, `window.presets`
 *    (the preset equal to the default marked "default"), `window.max_days` and
 *    `window.sustained_min_days`.
 * 2. **Thresholds**: every key of `config/thresholds.yaml`, flattened to dotted keys such as
 *    `k1.min_clicks_floor` and sorted, so the screen reads like the file and
 *    `docs/recommendation-rules.md` (AAP 0.5.4).
 * 3. **HubSpot mapping**: the MQL and Qualified Lead bindings of `config/hubspot_mapping.yaml`,
 *    bound and shown separately and never combined (AAP 0.5.4.7), the booking sources and
 *    meeting outcome exclusions, and the bindings as resolved against the portal of HubSpot's
 *    active primary dataset and, while LinkedIn is simulated, of its companion CRM dataset
 *    (AAP 0.3.3). An unresolved binding reads "unavailable" with the API's reason, never `0`.
 * 4. **White paper registry**: `config/content_assets.yaml`, one row per asset with its
 *    canonical lead-form URNs and creative overrides.
 * 5. **Configuration hash**: `config_hash`, stored on every analysis run (AAP 0.1.1
 *    reproducibility).
 *
 * Data sources: `GET /config` through `useConfig`, and the latest default run's
 * `display_timezone` through `useDefaultRun`, the zone every instant on the page is shown in
 * (`GET /config` carries none). Only the resolved bindings tables and the refresh-failure notice
 * show instants, so the run is read only while one of them is on screen.
 *
 * States: a spinner until the first response settles; the five sections once it has loaded; the
 * sections with a "Settings could not be refreshed" notice while a later refetch has failed; and
 * "Settings unavailable" when the first read failed.
 *
 * Rules this file keeps:
 * - **Read-only**: the page renders no input, button or other control and calls no mutation.
 *   `DataTable` is never given `renderExpanded`, which would add toggle buttons.
 * - **No literals the API owns** (AAP 0.5.4.1): every threshold, window length, preset, stage
 *   label and asset shown comes from the response. Only the labels are fixed English text.
 * - **Values verbatim**: configured values are shown as configured (`0.95` stays "0.95"), never
 *   reformatted as percentages or money. The only formatted values are instants, each through
 *   `formatInstant` in `DISPLAY_TIMEZONE` (AAP 0.5.5): each bindings table's `verified_at`, whose
 *   ISO 8601 value stays in its `<time dateTime>`, and the refresh notice's last-loaded instant.
 * - **Server state only through `../../api/queries`** (AAP 0.11.1); nothing here fetches.
 * - **Design system only** (AAP 0.4.4): UI comes from the design-system index and layout only
 *   from `Stack` and `Inline`, with no raw tables or headings, no styles and no stylesheet. Text
 *   lines are `<div>`s, never `<p>`s, so a Stack gap token alone spaces them: a `<p>`'s
 *   user-agent margins would add space that no spacing token sets.
 * - **No router import**: `src/router.tsx` renders the page frame (`PageShell` with `TopBar` and
 *   `SideNav`) and imports this page, so the page renders only `PageShell.Title` and
 *   `PageShell.Section`s.
 * - **No alert**: nothing here carries `role="alert"`, which AAP 0.5.5 reserves for the
 *   open-discrepancies banner. A failure is announced through a polite status region.
 *
 * @packageDocumentation
 */
import { useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import { isApiError, useConfig, useDefaultRun, type ApiError } from '../../api/queries';
import type {
  ConfigView,
  ContentAssetView,
  CrmBindingsView,
  HubSpotMappingView,
  StageBindingView,
  WindowConfig,
} from '../../api/types';
import {
  Badge,
  Banner,
  DataTable,
  EmptyState,
  Inline,
  PageShell,
  Spinner,
  Stack,
  withTokenBreaks,
  type DataTableColumn,
} from '../../design-system';
import { useDocumentTitle } from '../../lib/documentTitle';
import { formatInstant } from '../../lib/format';

// ---------------------------------------------------------------------------------------------
// Texts
// ---------------------------------------------------------------------------------------------

/** The page title, rendered as the page's single `<h1>` in every state. */
const PAGE_TITLE = 'Settings';

/** The note under the title, shown in every state. */
const READ_ONLY_NOTE = 'Read-only. Change the files in config/ and restart the services.';

/** Accessible label of the spinner shown until the configuration arrives. */
const LOADING_LABEL = 'Loading settings';

/** Title of the failure state. */
const LOAD_ERROR_TITLE = 'Settings unavailable';

/** Reason shown for a failure that carries no usable message of its own. */
const GENERIC_ERROR_MESSAGE = 'The configuration could not be loaded.';

/** Title of the notice shown while the cached configuration stays after a failed refetch. */
const REFRESH_ERROR_TITLE = 'Settings could not be refreshed';

/**
 * The zone the page's instants (each bindings table's `verified_at` and the refresh notice's
 * last-loaded instant) are shown in until the latest default run reports `display_timezone`
 * (`GET /runs/latest` answers 404 `not_found` before the first analysis). `formatInstant` labels
 * every instant with its zone, so the fallback is never mistaken for the configured one.
 */
const FALLBACK_TIMEZONE = 'UTC';

/** Shown for an absent value: a label, property or stage id the configuration does not set. */
const EM_DASH = '—';

/** Shown for an empty list or a `null` threshold. */
const NONE = 'none';

/** Separator of list values rendered on one line. */
const LIST_SEPARATOR = ', ';

/** Accessible name of the window presets list. */
const PRESETS_LIST_LABEL = 'Window presets';

/** The marker on the preset equal to `window.default_days`. */
const DEFAULT_PRESET_MARKER = 'default';

/** Shown beside the MQL and Qualified Lead bindings table (AAP 0.5.4.7, never blended). */
const NEVER_BLENDED_NOTE = 'MQL and Qualified Lead are bound separately and never combined.';

/** Accessible name of the group holding the booking sources and meeting outcome exclusions. */
const MEETING_RULES_LABEL = 'Meeting rules';

/** Shown while no HubSpot dataset has verified its bindings. */
const NOT_RESOLVED_NOTE =
  'Not resolved yet. The HubSpot dataset records them when it verifies its properties at startup.';

/** Title and caption of the resolved bindings of HubSpot's active primary dataset. */
const PRIMARY_BINDINGS_TITLE = 'Resolved stage properties';
const PRIMARY_BINDINGS_CAPTION = 'Resolved stage properties (HubSpot)';

/** Title and caption of the resolved bindings of the active companion CRM dataset. */
const COMPANION_BINDINGS_TITLE = 'Resolved stage properties (simulated companion)';

/** Shown beside the companion's bindings (AAP 0.3.3, effective CRM dataset). */
const COMPANION_NOTE = 'Used for LinkedIn-attributed evidence while LinkedIn data is simulated.';

/**
 * Shown in the Reason cell of an unavailable binding whose reason the API left empty, so the
 * cell is never blank (AAP 0.5.4.7).
 */
const NO_REASON_RECORDED = 'No reason recorded';

/** Shown under the configuration hash. */
const CONFIG_HASH_NOTE =
  "Stored on every analysis run, so a run's evidence can be traced to this configuration.";

// ---------------------------------------------------------------------------------------------
// Value helpers
// ---------------------------------------------------------------------------------------------

/**
 * A window length as text: "{n} days", or "1 day" for a single day.
 *
 * @param days - a configured window length, in days
 * @returns the length with its unit
 */
function daysText(days: number): string {
  // A singular unit for a single day: grammar, not a window length.
  return days === 1 ? `${String(days)} day` : `${String(days)} days`;
}

/**
 * List values joined on one line, or a placeholder for an empty list.
 *
 * @param values - the values, in configured order
 * @param empty - the text shown when there are none
 * @returns the joined values, or `empty`
 */
function joinList(values: readonly string[], empty: string): string {
  return values.length === 0 ? empty : values.join(LIST_SEPARATOR);
}

/**
 * Whether a string has visible text, so a blank configured value is shown as absent rather than
 * as an empty cell.
 *
 * @param value - a configured or resolved value
 * @returns `true` for a string with at least one non-whitespace character
 */
function hasText(value: string | null): value is string {
  return value !== null && value.trim() !== '';
}

/**
 * An identifier (a property name, a stage id) in `<code>`, or "—" when it is absent. A long
 * identifier breaks after its separators ({@link withTokenBreaks}), such as
 * `hs_v2_date_entered_` / `marketingqualifiedlead`, and never inside one of its parts; its text
 * is unchanged.
 *
 * @param value - the identifier, or `null`
 * @returns the cell content
 */
function codeOrDash(value: string | null): ReactNode {
  return hasText(value) ? <code>{withTokenBreaks(value)}</code> : EM_DASH;
}

/**
 * The reason a configuration read failed: the API's own message, trimmed, when the error is an
 * `ApiError` with a non-blank message, and a generic sentence otherwise, so the page never shows
 * an empty reason or the text "undefined".
 *
 * @param error - the query's error
 * @returns the non-empty reason text
 */
function failureReason(error: unknown): string {
  if (isApiError(error)) {
    const message = error.message.trim();
    if (message !== '') {
      return message;
    }
  }
  return GENERIC_ERROR_MESSAGE;
}

/**
 * The zone the page formats instants in (AAP 0.5.5 Behaviour: "Instants are formatted in
 * `DISPLAY_TIMEZONE`"): the latest default run's `display_timezone`, trimmed, because
 * `GET /config` carries no zone. While that run's read is pending, on its 404 `not_found` before
 * the first analysis, or when it names a blank zone, it is {@link FALLBACK_TIMEZONE}.
 *
 * Every caller observes the one `useDefaultRun` query (`queryKeys.runs.latest`), so TanStack
 * Query dedupes their reads and they all show the same zone.
 *
 * @returns the IANA zone id to pass to `formatInstant`
 */
function useDisplayTimezone(): string {
  const configuredZone = useDefaultRun().data?.display_timezone;
  return configuredZone !== undefined && hasText(configuredZone)
    ? configuredZone.trim()
    : FALLBACK_TIMEZONE;
}

/**
 * The error the configuration read last settled with while it holds no data, kept while the
 * same read runs again.
 *
 * TanStack Query v5 puts a query that holds no data back to `status: 'pending'` with
 * `error: null` each time it fetches. During an outage `useConfig` polls every
 * `CURRENT_STATE_POLL_MS` and retries each poll, so without this hook every attempt would swap
 * "Settings unavailable" for the loading spinner and back: the screen would flicker, and the
 * polite status region would announce the failure again each time (WCAG 2.2 SC 4.1.3). The
 * failure is kept until the read settles again, with a new error that replaces it or with data
 * that clears it. Before any read has settled there is nothing to keep, so the first load still
 * shows the spinner.
 *
 * The state is adjusted while rendering, React's pattern for information kept from earlier
 * renders; the guard makes the second render a no-op.
 *
 * @param error - the query's `error`
 * @param fetching - whether the query is fetching or paused waiting for the network
 * @param hasData - whether the query holds data
 * @returns the failure to present, or `null` while there is data, before any read failed, or
 *   once a read settled without an error
 */
function useSettledFailure(
  error: ApiError | null,
  fetching: boolean,
  hasData: boolean,
): ApiError | null {
  const [kept, setKept] = useState<ApiError | null>(null);
  const settled = hasData ? null : error === null && fetching ? kept : error;
  if (settled !== kept) {
    setKept(settled);
  }
  return settled;
}

// ---------------------------------------------------------------------------------------------
// Thresholds
// ---------------------------------------------------------------------------------------------

/** One threshold as the table shows it: its dotted key and its configured value as text. */
interface ThresholdEntry {
  /** The dotted key, for example `k1.min_clicks_floor`. */
  key: string;
  /** The configured value, verbatim. */
  value: string;
}

/** One row of the thresholds table: a {@link ThresholdEntry} with a row key unique in the table. */
interface ThresholdRow extends ThresholdEntry {
  /** The row's React key: its position and dotted key, unique even if two keys flatten alike. */
  id: string;
}

/**
 * Whether a JSON value is an object with named keys (a threshold namespace), as opposed to a
 * scalar, an array or `null`. It narrows `unknown` without a cast.
 *
 * @param value - one node of the thresholds tree
 * @returns `true` for a non-array object
 */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * One configured value as text, verbatim: numbers through `String(n)` (so `0.95` stays "0.95",
 * never "95%"), booleans as "true" or "false", strings as they are, `null` as "none", and arrays
 * as their values joined by ", " ("none" when empty). An object inside an array has no dotted
 * key of its own, so it is shown as its JSON.
 *
 * @param value - a leaf of the thresholds tree
 * @returns the value's text
 */
function formatThresholdValue(value: unknown): string {
  switch (typeof value) {
    case 'number':
    case 'bigint':
      return String(value);
    case 'boolean':
      return value ? 'true' : 'false';
    case 'string':
      return value;
    case 'undefined':
      return NONE;
    case 'object':
      if (value === null) {
        return NONE;
      }
      if (Array.isArray(value)) {
        return value.length === 0 ? NONE : value.map(formatThresholdValue).join(LIST_SEPARATOR);
      }
      return JSON.stringify(value);
    default:
      // A symbol or a function never comes from JSON; it is still shown rather than dropped.
      return String(value);
  }
}

/**
 * Compares two entries by key in UTF-16 code-unit order, not `localeCompare`, so the order is
 * the same under every locale.
 *
 * @param a - one entry
 * @param b - the other entry
 * @returns a negative number, zero or a positive number, as `Array.prototype.sort` expects
 */
function compareByKey(a: ThresholdEntry, b: ThresholdEntry): number {
  return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
}

/**
 * Flattens the thresholds tree into dotted keys with their values, sorted by key.
 *
 * - Plain objects are namespaces: their keys are joined to the prefix with "."
 *   (`{ k1: { min_clicks_floor: 10 } }` gives `k1.min_clicks_floor`), and a key that is
 *   already dotted is kept as it is.
 * - Every other value is a leaf, rendered by {@link formatThresholdValue}
 *   (`[14, 28, 56, 90]` gives "14, 28, 56, 90").
 * - An empty namespace below the root is one row whose value is "none", so a configured key is
 *   never silently dropped. The root itself, or a root that is not an object, gives no rows.
 *
 * @param value - the thresholds tree, or one node of it
 * @param prefix - the dotted key of `value`; omitted for the root
 * @returns the entries, sorted by {@link compareByKey}
 */
function flattenThresholds(
  value: unknown,
  prefix?: string,
): readonly { key: string; value: string }[] {
  if (!isPlainObject(value)) {
    return prefix === undefined ? [] : [{ key: prefix, value: formatThresholdValue(value) }];
  }
  const children = Object.entries(value);
  if (children.length === 0) {
    return prefix === undefined ? [] : [{ key: prefix, value: NONE }];
  }
  return children
    .flatMap(([name, child]) =>
      flattenThresholds(child, prefix === undefined ? name : `${prefix}.${name}`),
    )
    .sort(compareByKey);
}

/**
 * The thresholds table's rows: {@link flattenThresholds} of the tree, each with a unique row key.
 *
 * @param thresholds - `ConfigView.thresholds`
 * @returns the rows, sorted by key
 */
function thresholdRows(thresholds: ConfigView['thresholds']): readonly ThresholdRow[] {
  return flattenThresholds(thresholds).map((entry, index) => ({
    ...entry,
    id: `${String(index)}:${entry.key}`,
  }));
}

/**
 * The thresholds table's columns: the dotted key as the row header, then the value. A long key
 * breaks after its dots and underscores ({@link withTokenBreaks}), so it wraps between its parts
 * inside a narrow region rather than widening the table past it (WCAG 2.2 SC 1.4.10 Reflow), and
 * never inside one of them.
 */
const THRESHOLD_COLUMNS: readonly DataTableColumn<ThresholdRow>[] = [
  {
    key: 'key',
    header: 'Key',
    isRowHeader: true,
    render: (row) => <code>{withTokenBreaks(row.key)}</code>,
  },
  { key: 'value', header: 'Value' },
];

// ---------------------------------------------------------------------------------------------
// Analysis window
// ---------------------------------------------------------------------------------------------

/** One row of the window table. */
interface WindowRow {
  /** The setting's name, the row header. */
  label: string;
  /** Its key in `config/thresholds.yaml`. */
  key: string;
  /** Its configured value. */
  value: ReactNode;
}

/** Props of {@link PresetList}. */
interface PresetListProps {
  /** `window.presets`, in configured order. */
  presets: readonly number[];
  /** `window.default_days`. */
  defaultDays: number;
}

/**
 * The window presets as a list labelled "Window presets", each item "{n} days" in configured
 * order. The first preset equal to `window.default_days` also carries a "default" badge, so at
 * most one item is marked, and none when no preset equals the default.
 */
function PresetList({ presets, defaultDays }: PresetListProps): ReactNode {
  if (presets.length === 0) {
    return NONE;
  }
  const defaultIndex = presets.indexOf(defaultDays);
  return (
    <Stack as="ul" gap={1} aria-label={PRESETS_LIST_LABEL}>
      {presets.map((days, index) => (
        <li key={`${String(index)}:${String(days)}`}>
          {index === defaultIndex ? (
            <Inline gap={2}>
              <span>{daysText(days)}</span>{' '}
              <Badge tone="info">{DEFAULT_PRESET_MARKER}</Badge>
            </Inline>
          ) : (
            daysText(days)
          )}
        </li>
      ))}
    </Stack>
  );
}

/**
 * The window table's rows, in the order the window settings are read: default, trusted minimum,
 * presets, maximum and sustained-pattern minimum.
 *
 * @param settings - `ConfigView.window`
 * @returns the five rows
 */
function windowRows(settings: WindowConfig): readonly WindowRow[] {
  return [
    {
      label: 'Default window',
      key: 'window.default_days',
      value: daysText(settings.default_days),
    },
    {
      label: 'Trusted minimum',
      key: 'window.trusted_min_days',
      value: daysText(settings.trusted_min_days),
    },
    {
      label: 'Presets',
      key: 'window.presets',
      value: <PresetList presets={settings.presets} defaultDays={settings.default_days} />,
    },
    { label: 'Maximum window', key: 'window.max_days', value: daysText(settings.max_days) },
    {
      label: 'Sustained-pattern minimum',
      key: 'window.sustained_min_days',
      value: daysText(settings.sustained_min_days),
    },
  ];
}

/**
 * The window table's columns: the setting as the row header, its key and its value. A long key
 * breaks after its separators, as in the thresholds table.
 */
const WINDOW_COLUMNS: readonly DataTableColumn<WindowRow>[] = [
  { key: 'label', header: 'Setting', isRowHeader: true },
  { key: 'key', header: 'Key', render: (row) => <code>{withTokenBreaks(row.key)}</code> },
  { key: 'value', header: 'Value', render: (row) => row.value },
];

// ---------------------------------------------------------------------------------------------
// HubSpot mapping and resolved bindings
// ---------------------------------------------------------------------------------------------

/** One row of the configured MQL and Qualified Lead bindings table. */
interface MappingRow {
  /** The count the binding feeds, the row header. */
  count: 'MQL' | 'Qualified Lead';
  /** The binding as configured. */
  binding: StageBindingView;
}

/**
 * The configured bindings' columns. MQL's single `stage_label` arrives as a one-element
 * `stage_labels` list, so both rows render their labels the same way. In `stage_label` mode the
 * labels are tried in order; in `property` mode any configured labels are kept for display only
 * and shown as configured. The cell shows "—" only when the binding has no label. A long mode or
 * property identifier breaks after its separators, so it wraps between its parts in a narrow
 * region and never inside one.
 */
const MAPPING_COLUMNS: readonly DataTableColumn<MappingRow>[] = [
  { key: 'count', header: 'Count', isRowHeader: true },
  {
    key: 'mode',
    header: 'Mode',
    render: (row) => <code>{withTokenBreaks(row.binding.mode)}</code>,
  },
  {
    key: 'stage_labels',
    header: 'Stage label(s)',
    render: (row) => joinList(row.binding.stage_labels.filter(hasText), EM_DASH),
  },
  {
    key: 'property',
    header: 'Property',
    render: (row) => codeOrDash(row.binding.property),
  },
];

/** One row of a resolved bindings table. */
interface BindingRow {
  /** The row key. */
  id: 'mql' | 'ql' | 'google_campaign' | 'google_touch_date';
  /** The binding's name, the row header. */
  binding: string;
  /** Whether the binding resolved, so its count or link can be computed. */
  available: boolean;
  /**
   * The stage label this row's own binding resolved: the matched label in `stage_label` mode, or a
   * configured display label kept in `property` mode; `null` when none and for Google link rows.
   */
  matchedLabel: string | null;
  /** The lifecycle stage id the label resolved to; `null` when there is none. */
  stageId: string | null;
  /** The contact property read. */
  property: string | null;
  /** Why the binding is unavailable; set when unavailable. */
  reason: string | null;
}

/**
 * One row per binding a {@link CrmBindingsView} defines: MQL and Qualified Lead, each resolved on
 * its own (QL is never derived from MQL), then the two properties of the optional Google-to-CRM
 * link, which share its availability and reason. Used for the primary and the companion
 * bindings alike.
 *
 * @param bindings - the bindings as one HubSpot dataset verified them
 * @returns the four rows
 */
function bindingRows(bindings: CrmBindingsView): readonly BindingRow[] {
  const { mql, ql, google } = bindings;
  return [
    {
      id: 'mql',
      binding: 'MQL',
      available: mql.available,
      matchedLabel: mql.label,
      stageId: mql.stage_id,
      property: mql.property,
      reason: mql.reason,
    },
    {
      id: 'ql',
      binding: 'Qualified Lead',
      available: ql.available,
      matchedLabel: ql.label,
      stageId: ql.stage_id,
      property: ql.property,
      reason: ql.reason,
    },
    {
      id: 'google_campaign',
      binding: 'Google campaign property',
      available: google.available,
      matchedLabel: null,
      stageId: null,
      property: google.campaign_property,
      reason: google.reason,
    },
    {
      id: 'google_touch_date',
      binding: 'Google touch-date property',
      available: google.available,
      matchedLabel: null,
      stageId: null,
      property: google.touch_date_property,
      reason: google.reason,
    },
  ];
}

/**
 * A resolved bindings table's columns. An unavailable binding shows its "unavailable" badge,
 * "—" for the matched label, stage id and property, and the API's reason: never `0` and never an
 * empty cell (AAP 0.5.4.7).
 *
 * The "Matched label" column shows, in each row, the label its own binding resolved: MQL's for MQL
 * and Qualified Lead's for Qualified Lead, or "—" for the Google link and for a property binding
 * that keeps no display label. Qualified Lead tries its configured labels in order, so its row
 * states which one the portal resolved. The two bindings resolve independently, and only the MQL
 * binding's label heads the white-paper MQL column.
 *
 * The property and reason columns hold long identifiers, such as
 * `hs_v2_date_entered_marketingqualifiedlead` or `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY` in a
 * reason. Like every cell they wrap only between words, and an identifier only after its
 * underscores ({@link withTokenBreaks}, which `DataTable` applies to the reason's text and
 * {@link codeOrDash} to the property), so its longest part, not the whole identifier, sets the
 * column's minimum width, and no part ever breaks inside itself. Each takes a third of the
 * table's width, so wherever the table has room a property such as
 * `hs_v2_date_entered_990100000` and the reason's variable names stay on one line, and only in
 * a narrow region do they wrap. Each also holds the `'md'` minimum: in a region too narrow for
 * the table's other columns, where the table scrolls anyway, an identifier then wraps into a few
 * readable parts rather than one part per line. The binding takes a sixth, so a name such as
 * "Google touch-date property" keeps two lines rather than a word per line. The stage id never
 * breaks: a lifecycle stage id is a numeric id such as `990100000` or a HubSpot stage name of at
 * most 22 characters, such as `marketingqualifiedlead`, with no separator, short enough that the
 * table still fits a desktop region with it whole.
 */
const BINDING_COLUMNS: readonly DataTableColumn<BindingRow>[] = [
  { key: 'binding', header: 'Binding', isRowHeader: true, share: 2 },
  {
    key: 'status',
    header: 'Status',
    render: (row) =>
      row.available ? (
        <Badge tone="success">resolved</Badge>
      ) : (
        <Badge tone="warning">unavailable</Badge>
      ),
  },
  {
    key: 'matched_label',
    header: 'Matched label',
    render: (row) => (row.available && hasText(row.matchedLabel) ? row.matchedLabel : EM_DASH),
  },
  {
    key: 'stage_id',
    header: 'Stage id',
    render: (row) => (row.available ? codeOrDash(row.stageId) : EM_DASH),
  },
  {
    key: 'property',
    header: 'Property',
    share: 4,
    minWidth: 'md',
    render: (row) => (row.available ? codeOrDash(row.property) : EM_DASH),
  },
  {
    key: 'reason',
    header: 'Reason',
    share: 4,
    minWidth: 'md',
    render: (row) => {
      if (hasText(row.reason)) {
        return row.reason;
      }
      return row.available ? EM_DASH : NO_REASON_RECORDED;
    },
  },
];

/** Props of {@link BindingsTable}. */
interface BindingsTableProps {
  /** The bindings as one HubSpot dataset verified them. */
  bindings: CrmBindingsView;
  /** The table's caption, unique on the page. */
  caption: string;
}

/**
 * One dataset's resolved bindings: the table, then the instant the portal was checked, formatted
 * in the display time zone ({@link useDisplayTimezone}; AAP 0.5.5 Behaviour), for example
 * "Verified against the portal at Oct 10, 2026, 12:50 PM America/New_York." The API's ISO 8601
 * value stays machine-readable in the `<time>` element's `dateTime`.
 */
function BindingsTable({ bindings, caption }: BindingsTableProps): ReactElement {
  const displayTimezone = useDisplayTimezone();
  return (
    <Stack gap={2}>
      <DataTable
        caption={caption}
        columns={BINDING_COLUMNS}
        rows={bindingRows(bindings)}
        getRowKey={(row) => row.id}
      />
      <div>
        Verified against the portal at{' '}
        <time dateTime={bindings.verified_at}>
          {formatInstant(bindings.verified_at, displayTimezone)}
        </time>
        .
      </div>
    </Stack>
  );
}

/** Props of {@link MeetingRule}. */
interface MeetingRuleProps {
  /** The rule's label. */
  label: string;
  /** Its configured values, in order. */
  values: readonly string[];
}

/**
 * One labelled meeting rule: the label, then its configured values joined by ", " in `<code>`,
 * or "none" when the list is empty.
 */
function MeetingRule({ label, values }: MeetingRuleProps): ReactElement {
  const shown = values.filter(hasText);
  return (
    <Inline gap={2}>
      <span>{label}</span>
      {shown.length === 0 ? <span>{NONE}</span> : <code>{joinList(shown, NONE)}</code>}
    </Inline>
  );
}

// ---------------------------------------------------------------------------------------------
// White paper registry
// ---------------------------------------------------------------------------------------------

/** Props of {@link UrnList}. */
interface UrnListProps {
  /** The URNs, in configured order. */
  urns: readonly string[];
  /** The list's accessible name, naming the asset it belongs to. */
  label: string;
}

/**
 * URNs as a list, each in `<code>` and breaking after its colons ({@link withTokenBreaks}), or
 * "—" when there are none.
 */
function UrnList({ urns, label }: UrnListProps): ReactNode {
  if (urns.length === 0) {
    return EM_DASH;
  }
  return (
    <Stack as="ul" gap={1} aria-label={label}>
      {urns.map((urn, index) => (
        <li key={`${String(index)}:${urn}`}>
          <code>{withTokenBreaks(urn)}</code>
        </li>
      ))}
    </Stack>
  );
}

/**
 * The registry table's columns: the slug as the row header, the title and both URN lists. A long
 * slug or URN breaks after its separators ({@link withTokenBreaks}), so it wraps between its parts
 * in a narrow region ("urn:li:" / "leadGenForm:7001") and never inside one; the title breaks only
 * between words. The title takes a sixth of the table's width and wraps within it, which leaves
 * the rest to the slug and URN columns, so at desktop widths a slug and a URN such as
 * `urn:li:sponsoredCreative:990001301` stay on one line rather than sharing the squeeze.
 */
const REGISTRY_COLUMNS: readonly DataTableColumn<ContentAssetView>[] = [
  {
    key: 'slug',
    header: 'Slug',
    isRowHeader: true,
    render: (asset) => <code>{withTokenBreaks(asset.slug)}</code>,
  },
  { key: 'title', header: 'Title', share: 2 },
  {
    key: 'lead_forms',
    header: 'Lead forms',
    render: (asset) => <UrnList urns={asset.lead_forms} label={`Lead forms of ${asset.slug}`} />,
  },
  {
    key: 'creative_overrides',
    header: 'Creative overrides',
    render: (asset) => (
      <UrnList urns={asset.creative_overrides} label={`Creative overrides of ${asset.slug}`} />
    ),
  },
];

// ---------------------------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------------------------

/** The "Analysis window" section, from `config.window` only. */
function WindowSection({ settings }: { settings: WindowConfig }): ReactElement {
  return (
    <PageShell.Section id="settings-window" title="Analysis window">
      <DataTable
        caption="Analysis window settings"
        columns={WINDOW_COLUMNS}
        rows={windowRows(settings)}
        getRowKey={(row) => row.key}
      />
    </PageShell.Section>
  );
}

/** The "Thresholds" section: every configured key, dotted and sorted. */
function ThresholdsSection({
  thresholds,
}: {
  thresholds: ConfigView['thresholds'];
}): ReactElement {
  return (
    <PageShell.Section id="settings-thresholds" title="Thresholds">
      <DataTable
        caption="Thresholds"
        columns={THRESHOLD_COLUMNS}
        rows={thresholdRows(thresholds)}
        getRowKey={(row) => row.id}
        emptyMessage="No thresholds configured."
      />
    </PageShell.Section>
  );
}

/** Props of {@link HubSpotSection}. */
interface HubSpotSectionProps {
  /** `config.hubspot_mapping`: the bindings and meeting rules as configured. */
  mapping: HubSpotMappingView;
  /** `config.crm_bindings`: resolved against HubSpot's active primary dataset, or `null`. */
  bindings: CrmBindingsView | null;
  /** `config.companion_crm_bindings`: resolved against the companion CRM dataset, or `null`. */
  companion: CrmBindingsView | null;
}

/**
 * The "HubSpot mapping" section: the configured MQL and Qualified Lead bindings, the meeting
 * rules, then the bindings as resolved, in their own level-3 sections. The companion's section
 * appears only while a companion CRM dataset is active.
 */
function HubSpotSection({ mapping, bindings, companion }: HubSpotSectionProps): ReactElement {
  const mappingRows: readonly MappingRow[] = [
    { count: 'MQL', binding: mapping.mql },
    { count: 'Qualified Lead', binding: mapping.ql },
  ];
  return (
    <PageShell.Section id="settings-hubspot" title="HubSpot mapping">
      <Stack gap={4}>
        <DataTable
          caption="MQL and Qualified Lead bindings"
          columns={MAPPING_COLUMNS}
          rows={mappingRows}
          getRowKey={(row) => row.count}
        />
        <div>{NEVER_BLENDED_NOTE}</div>
        {/* Named as a group, because the same lists also appear as `crm.*` rows of the
            thresholds table, where the API merges them for the rules that read them. */}
        <Stack gap={2} aria-label={MEETING_RULES_LABEL}>
          <MeetingRule label="Booking sources" values={mapping.booking_sources} />
          <MeetingRule
            label="Meeting outcome exclusions"
            values={mapping.meeting_outcome_exclusions}
          />
        </Stack>
        <PageShell.Section
          id="settings-crm-bindings"
          title={PRIMARY_BINDINGS_TITLE}
          headingLevel={3}
        >
          {bindings === null ? (
            <div>{NOT_RESOLVED_NOTE}</div>
          ) : (
            <BindingsTable bindings={bindings} caption={PRIMARY_BINDINGS_CAPTION} />
          )}
        </PageShell.Section>
        {companion !== null && (
          <PageShell.Section
            id="settings-companion-crm-bindings"
            title={COMPANION_BINDINGS_TITLE}
            headingLevel={3}
          >
            <Stack gap={2}>
              <div>{COMPANION_NOTE}</div>
              <BindingsTable bindings={companion} caption={COMPANION_BINDINGS_TITLE} />
            </Stack>
          </PageShell.Section>
        )}
      </Stack>
    </PageShell.Section>
  );
}

/** The "White paper registry" section, in file order. */
function RegistrySection({ assets }: { assets: readonly ContentAssetView[] }): ReactElement {
  return (
    <PageShell.Section id="settings-registry" title="White paper registry">
      <DataTable
        caption="White paper registry"
        columns={REGISTRY_COLUMNS}
        rows={assets}
        getRowKey={(asset) => asset.slug}
        emptyMessage="No white papers registered."
      />
    </PageShell.Section>
  );
}

/** The "Configuration hash" section. The hash may break anywhere, so it wraps on narrow screens. */
function ConfigHashSection({ hash }: { hash: string }): ReactElement {
  return (
    <PageShell.Section id="settings-config-hash" title="Configuration hash">
      <Stack gap={2} breakLongWords>
        <div>
          <code>{hash}</code>
        </div>
        <div>{CONFIG_HASH_NOTE}</div>
      </Stack>
    </PageShell.Section>
  );
}

/** The loaded page: the five sections, in order. */
function SettingsContent({ config }: { config: ConfigView }): ReactElement {
  return (
    <Stack gap={6}>
      <WindowSection settings={config.window} />
      <ThresholdsSection thresholds={config.thresholds} />
      <HubSpotSection
        mapping={config.hubspot_mapping}
        bindings={config.crm_bindings}
        companion={config.companion_crm_bindings}
      />
      <RegistrySection assets={config.content_assets} />
      <ConfigHashSection hash={config.config_hash} />
    </Stack>
  );
}

/** Props of {@link RefreshFailureNotice}. */
interface RefreshFailureNoticeProps {
  /** The error of the refetch that failed while the last configuration stays on screen. */
  error: unknown;
  /** When the configuration on screen was loaded: the query's `dataUpdatedAt`, epoch ms. */
  dataUpdatedAt: number;
}

/**
 * The notice shown while a background refetch of `GET /config` has failed and the last
 * configuration stays on screen, so cached settings never read as current: a warning `Banner`
 * "Settings could not be refreshed" with the failure's reason and when the configuration on
 * screen was loaded, as an instant in `DISPLAY_TIMEZONE` (AAP 0.5.5 Behaviour).
 *
 * - The zone comes from {@link useDisplayTimezone}, like the bindings tables' instants: the
 *   latest default run's `display_timezone`. Until that run reports a non-blank zone (while its
 *   read is pending, or on its 404 before the first analysis), the instant is shown in
 *   {@link FALLBACK_TIMEZONE}, labelled as such.
 * - The banner is not an alert (AAP 0.5.5 reserves `role="alert"` for discrepancies). The page
 *   renders it inside its polite status region, which announces it.
 * - It offers no Retry, because the page has no controls. The query reads again by itself, on
 *   window focus, when the page mounts again and, after a transient failure (an outage), every
 *   `CURRENT_STATE_POLL_MS`; the notice leaves once a read succeeds.
 */
function RefreshFailureNotice({ error, dataUpdatedAt }: RefreshFailureNoticeProps): ReactElement {
  const displayTimezone = useDisplayTimezone();
  const loadedAt = formatInstant(new Date(dataUpdatedAt).toISOString(), displayTimezone);
  return (
    <Banner tone="warning" title={REFRESH_ERROR_TITLE}>
      {/* Lines, not paragraphs: feature code carries no CSS (AAP 0.4.4), so the Stack gap token
          alone spaces them, where a <p>'s user-agent margins no spacing token sets. */}
      <Stack gap={1}>
        <div>{failureReason(error)}</div>
        <div>{`Showing the configuration as last loaded ${loadedAt}.`}</div>
      </Stack>
    </Banner>
  );
}

// ---------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------

/**
 * Read-only view of the active configuration from `GET /api/v1/config` (AAP 0.5.5). Shows no
 * inputs and performs no mutations.
 *
 * It reads the configuration with one `useConfig()` call and renders, under the page's single
 * `<h1>` "Settings", which also titles the document "Settings · Ads Action Engine"
 * ({@link useDocumentTitle}, WCAG 2.2 SC 2.4.2), and the note "Read-only. Change the files in
 * config/ and restart the services.":
 *
 * - the five sections (analysis window, thresholds, HubSpot mapping, white paper registry and
 *   configuration hash) whenever the configuration is loaded;
 * - while a background refetch has failed and the last response stays on screen, above those
 *   sections the warning notice "Settings could not be refreshed" with the API's message and
 *   when the configuration on screen was loaded ({@link RefreshFailureNotice}), announced
 *   through a polite status region. It leaves once a later read succeeds;
 * - a `Spinner` labelled "Loading settings" until the first response settles;
 * - otherwise an `EmptyState` "Settings unavailable" with the API's message, announced through
 *   the same region.
 *
 * Neither failure offers an action, because the page has no controls: the query reads again by
 * itself on window focus or when the page mounts again, once its 5-minute freshness has passed,
 * and after a transient failure (no network, a 429 or a 5xx, as during an outage) every
 * `CURRENT_STATE_POLL_MS` until a read succeeds, so the page recovers by itself once the API
 * answers (`configQueryOptions`). While such a read runs again, "Settings unavailable" stays in
 * place, never replaced by the spinner or announced again, until the read settles with the
 * configuration or a new error ({@link useSettledFailure}).
 *
 * Instants (the bindings' verification and the notice's last-loaded time) are shown in the
 * latest default run's display time zone ({@link useDisplayTimezone}), so while either is on
 * screen the page also reads `GET /runs/latest`; nothing else is requested.
 *
 * The component must render inside the app's `QueryClientProvider`, as every authenticated page
 * does; `src/router.tsx` renders it inside the page frame at `/settings`.
 *
 * @returns the `/settings` page content, rendered inside the app's `PageShell`
 *
 * @example
 * ```tsx
 * // src/router.tsx, /settings route component:
 * return <SettingsPage />;
 * ```
 */
export function SettingsPage(): ReactElement {
  useDocumentTitle(PAGE_TITLE);
  const { data, dataUpdatedAt, error, fetchStatus, isPending } = useConfig();

  // A failed read that holds no data, kept on screen while a poll or retry reads again.
  const failure = useSettledFailure(error, fetchStatus !== 'idle', data !== undefined);
  // TanStack Query keeps the last data when a refetch fails and sets `error` beside it.
  const refreshFailed = data !== undefined && error !== null;

  return (
    <Stack gap={6}>
      <PageShell.Title>{PAGE_TITLE}</PageShell.Title>
      <div>{READ_ONLY_NOTE}</div>
      {/* One polite region rendered in every state, so a failure that replaces the spinner, or a
          failed refetch behind the cached sections, is announced (WCAG 2.2 SC 4.1.3); while
          empty, Stack takes it out of the column. */}
      <div role="status">
        {failure !== null ? (
          <EmptyState title={LOAD_ERROR_TITLE}>{failureReason(failure)}</EmptyState>
        ) : null}
        {refreshFailed ? (
          <RefreshFailureNotice error={error} dataUpdatedAt={dataUpdatedAt} />
        ) : null}
      </div>
      {data !== undefined ? (
        <SettingsContent config={data} />
      ) : (
        failure === null && isPending && <Spinner label={LOADING_LABEL} showLabel />
      )}
    </Stack>
  );
}
