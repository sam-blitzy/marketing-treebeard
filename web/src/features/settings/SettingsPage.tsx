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
 * Rules this file keeps:
 * - **Read-only**: the page renders no input, button or other control and calls no mutation.
 *   `DataTable` is never given `renderExpanded`, which would add toggle buttons.
 * - **No literals the API owns** (AAP 0.5.4.1): every threshold, window length, preset, stage
 *   label and asset shown comes from the response. Only the labels are fixed English text.
 * - **Values verbatim**: configured values are shown as configured (`0.95` stays "0.95"), never
 *   reformatted as percentages or money, so this file needs no formatting helpers.
 * - **Server state only through `../../api/queries`** (AAP 0.11.1); nothing here fetches.
 * - **Design system only** (AAP 0.4.4): UI comes from the design-system index and layout only
 *   from `Stack` and `Inline`, with no raw tables or headings, no styles and no stylesheet.
 * - **No router import**: `src/router.tsx` renders the page frame (`PageShell` with `TopBar` and
 *   `SideNav`) and imports this page, so the page renders only `PageShell.Title` and
 *   `PageShell.Section`s.
 * - **No alert**: nothing here carries `role="alert"`, which AAP 0.5.5 reserves for the
 *   open-discrepancies banner. A failure is announced through a polite status region.
 *
 * @packageDocumentation
 */
import type { ReactElement, ReactNode } from 'react';

import { isApiError, useConfig } from '../../api/queries';
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
  DataTable,
  EmptyState,
  Inline,
  PageShell,
  Spinner,
  Stack,
  type DataTableColumn,
} from '../../design-system';

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
 * An identifier (a property name, a stage id) in `<code>`, or "—" when it is absent.
 *
 * @param value - the identifier, or `null`
 * @returns the cell content
 */
function codeOrDash(value: string | null): ReactNode {
  return hasText(value) ? <code>{value}</code> : EM_DASH;
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

/** The thresholds table's columns: the dotted key as the row header, then the value. */
const THRESHOLD_COLUMNS: readonly DataTableColumn<ThresholdRow>[] = [
  { key: 'key', header: 'Key', isRowHeader: true, render: (row) => <code>{row.key}</code> },
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

/** The window table's columns: the setting as the row header, its key and its value. */
const WINDOW_COLUMNS: readonly DataTableColumn<WindowRow>[] = [
  { key: 'label', header: 'Setting', isRowHeader: true },
  { key: 'key', header: 'Key', render: (row) => <code>{row.key}</code> },
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
 * `stage_labels` list, so both rows render their labels the same way; a binding in `property`
 * mode has no labels and shows "—".
 */
const MAPPING_COLUMNS: readonly DataTableColumn<MappingRow>[] = [
  { key: 'count', header: 'Count', isRowHeader: true },
  { key: 'mode', header: 'Mode', render: (row) => <code>{row.binding.mode}</code> },
  {
    key: 'stage_labels',
    header: 'Stage label(s)',
    render: (row) => joinList(row.binding.stage_labels.filter(hasText), EM_DASH),
  },
  { key: 'property', header: 'Property', render: (row) => codeOrDash(row.binding.property) },
];

/** One row of a resolved bindings table. */
interface BindingRow {
  /** The row key. */
  id: 'mql' | 'ql' | 'google_campaign' | 'google_touch_date';
  /** The binding's name, the row header. */
  binding: string;
  /** Whether the binding resolved, so its count or link can be computed. */
  available: boolean;
  /** The lifecycle stage label that matched; `null` for property bindings and the Google link. */
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
 * The "Matched label" column goes beyond the five columns the screen's plan lists: Qualified
 * Lead tries its labels in order, and the label that matched is what heads the white-paper MQL
 * column, so the screen states which one the portal resolved.
 */
const BINDING_COLUMNS: readonly DataTableColumn<BindingRow>[] = [
  { key: 'binding', header: 'Binding', isRowHeader: true },
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
    render: (row) => (row.available ? codeOrDash(row.property) : EM_DASH),
  },
  {
    key: 'reason',
    header: 'Reason',
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
 * One dataset's resolved bindings: the table and the instant the portal was checked, shown as
 * the API returned it.
 */
function BindingsTable({ bindings, caption }: BindingsTableProps): ReactElement {
  return (
    <Stack gap={2}>
      <DataTable
        caption={caption}
        columns={BINDING_COLUMNS}
        rows={bindingRows(bindings)}
        getRowKey={(row) => row.id}
      />
      <p>
        Verified against the portal at{' '}
        <time dateTime={bindings.verified_at}>{bindings.verified_at}</time>.
      </p>
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

/** URNs as a list, each in `<code>`, or "—" when there are none. */
function UrnList({ urns, label }: UrnListProps): ReactNode {
  if (urns.length === 0) {
    return EM_DASH;
  }
  return (
    <Stack as="ul" gap={1} aria-label={label}>
      {urns.map((urn, index) => (
        <li key={`${String(index)}:${urn}`}>
          <code>{urn}</code>
        </li>
      ))}
    </Stack>
  );
}

/** The registry table's columns: the slug as the row header, the title and both URN lists. */
const REGISTRY_COLUMNS: readonly DataTableColumn<ContentAssetView>[] = [
  { key: 'slug', header: 'Slug', isRowHeader: true, render: (asset) => <code>{asset.slug}</code> },
  { key: 'title', header: 'Title' },
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
        <p>{NEVER_BLENDED_NOTE}</p>
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
            <p>{NOT_RESOLVED_NOTE}</p>
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
              <p>{COMPANION_NOTE}</p>
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
        <p>
          <code>{hash}</code>
        </p>
        <p>{CONFIG_HASH_NOTE}</p>
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

// ---------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------

/**
 * Read-only view of the active configuration from `GET /api/v1/config` (AAP 0.5.5). Shows no
 * inputs and performs no mutations.
 *
 * It reads the configuration with one `useConfig()` call and renders, under the page's single
 * `<h1>` "Settings" and the note "Read-only. Change the files in config/ and restart the
 * services.":
 *
 * - the five sections (analysis window, thresholds, HubSpot mapping, white paper registry and
 *   configuration hash) whenever the configuration is loaded, including after a failed
 *   background refetch, which keeps the last response;
 * - a `Spinner` labelled "Loading settings" until the first response settles;
 * - otherwise an `EmptyState` "Settings unavailable" with the API's message, announced through
 *   a polite status region. It offers no action, because the page has no controls.
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
  const { data, error, isPending } = useConfig();

  const failed = data === undefined && !isPending;

  return (
    <Stack gap={6}>
      <PageShell.Title>{PAGE_TITLE}</PageShell.Title>
      <p>{READ_ONLY_NOTE}</p>
      {/* One polite region rendered in every state, so a failure that replaces the spinner is
          announced (WCAG 2.2 SC 4.1.3); while empty, Stack takes it out of the column. */}
      <div role="status">
        {failed ? (
          <EmptyState title={LOAD_ERROR_TITLE}>{failureReason(error)}</EmptyState>
        ) : null}
      </div>
      {data !== undefined ? (
        <SettingsContent config={data} />
      ) : (
        isPending && <Spinner label={LOADING_LABEL} />
      )}
    </Stack>
  );
}
