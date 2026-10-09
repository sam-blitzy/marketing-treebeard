/**
 * The decision log on `/decisions` (AAP 0.5.5 "`DecisionLog` lists decisions per subject
 * (campaign or shared budget), with origin, time and the patch applied"; FR-10).
 *
 * What it shows, from `GET /decisions` through {@link useDecisions} with no filter: the decisions
 * of every source's active dataset, newest first (at most the API's default 200 rows). Rows are
 * grouped by subject, a campaign or a Google explicitly shared budget, keyed by subject type and
 * id, because ids are unique only within a type. Groups follow their newest decision, which is
 * the order a subject first appears in the newest-first list, and each group keeps its rows
 * newest first, so its first row is the subject's "last decided" state (AAP 0.5.2).
 *
 * Each subject is one `PageShell.Section` (an `<h3>` under the page's "Decision log" `<h2>`)
 * with a "Record decision" action that opens `DecisionForm` in a dialog, and one captioned
 * `DataTable` with these columns:
 *
 * 1. **Decided**: the instant, in the display time zone of the latest default run.
 * 2. **Origin**: Baseline, User, Recommendation or Resolution; a row recorded from a
 *    recommendation reads "Recommendation".
 * 3. **Decided by**: the user, or "System" for rows the product wrote itself.
 * 4. **Patch applied**: the fields the submission itself carried, in the fixed order status,
 *    daily budget, total budget ("status: paused; daily budget: €120.00").
 * 5. **Resulting expected state**: the complete materialised state the row holds, rendered by
 *    its decision shape (`own_budget`, `shared_member`, `budget`; AAP 0.5.2).
 * 6. **Note**.
 *
 * Rules this file keeps:
 *
 * - **Append-only, as stored.** Rows are rendered exactly as the API returns them; nothing is
 *   merged, recomputed or reordered across subjects.
 * - **One currency per subject.** Every amount is formatted in its own subject's currency, and
 *   no amount is ever summed or compared across subjects (AAP 0.1.1).
 * - **Stored JSON is read defensively.** A row's `patch` is stored `jsonb`: only the keys the
 *   contract defines are read, and only when they hold a non-blank string.
 * - **Server state only through `../../api/queries`** (AAP 0.11.1) and UI only from the
 *   design-system index, with no styles of this module's own (AAP 0.4.4).
 *
 * @example
 * ```tsx
 * <PageShell.Section id="decision-log" title="Decision log">
 *   <DecisionLog />
 * </PageShell.Section>
 * ```
 *
 * @packageDocumentation
 */
import { useState } from 'react';
import type { ReactNode } from 'react';

import { useDecisions, useDefaultRun } from '../../api/queries';
import type {
  CampaignStatus,
  Decision,
  DecisionOrigin,
  DecisionPatchView,
  SubjectIn,
  SubjectType,
  SubjectView,
} from '../../api/types';
import {
  Button,
  DataTable,
  Dialog,
  EmptyState,
  PageShell,
  Spinner,
  Stack,
  type DataTableColumn,
} from '../../design-system';
import { EM_DASH, formatInstant, formatMoney, sourceLabel } from '../../lib/format';
import { CAMPAIGN_STATUS_LABELS, DecisionForm } from './DecisionForm';

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/**
 * The zone instants are shown in until the latest default run names `DISPLAY_TIMEZONE`, for
 * example before the first run succeeds. `formatInstant` labels every instant with its zone, so
 * the fallback is never mistaken for local time.
 */
const FALLBACK_TIMEZONE = 'UTC';

/**
 * The most rows `GET /decisions` returns without a `limit` (`DEFAULT_LIMIT` in the backend's
 * `api/routes/decisions.py`). A log of exactly this many rows may omit older decisions, and says
 * so.
 */
const DECISION_LOG_LIMIT = 200;

/** Separator between the parts of a subject title and of an expected state. */
const PART_SEPARATOR = ' · ';

/** Separator between the fields of a patch. */
const PATCH_SEPARATOR = '; ';

/** The title of the dialog that hosts `DecisionForm`; the e2e suite finds the dialog by it. */
const DIALOG_TITLE = 'Record decision';

/** The label of each subject type, as the subject title shows it. */
const SUBJECT_TYPE_LABELS: Readonly<Record<SubjectType, string>> = Object.freeze({
  campaign: 'Campaign',
  budget: 'Shared budget',
});

/** The label of each decision origin (`db.models.decisions.DECISION_ORIGINS`). */
const ORIGIN_LABELS: Readonly<Record<DecisionOrigin, string>> = Object.freeze({
  baseline: 'Baseline',
  user: 'User',
  recommendation: 'Recommendation',
  resolution: 'Resolution',
});

/** Shown as the author of a row with no `decided_by`: baseline and other system-written rows. */
const SYSTEM_AUTHOR = 'System';

/** The patch cell of a baseline row, whose patch is empty because no submission wrote it. */
const BASELINE_PATCH_TEXT = 'Platform state at first sync';

/** The expected state of a row recording an acknowledged removal (`tombstone`). */
const REMOVAL_TEXT = 'Removal acknowledged';

/** Shown when a row carries no status where its shape owns one. */
const STATUS_NOT_REPORTED = 'status not reported';

/** Shown when a shared budget's row carries neither amount. */
const NO_AMOUNT_TEXT = 'no amount recorded';

/** Shown when the decision log could not be loaded and the API sent no message. */
const LOAD_ERROR_FALLBACK = 'The decision log request failed.';

/**
 * A budget name as the API builds it for a shared budget subject ("Shared budget {external
 * id}", `_SHARED_BUDGET_NAME` in `api/routes/decisions.py`). Its prefix is dropped where the
 * text already says "shared budget", so a member reads "shared budget 4455", not
 * "shared budget Shared budget 4455".
 */
const SHARED_BUDGET_NAME_PREFIX = /^shared budget\s+/i;

/** Characters an HTML id built from a subject keeps; any other becomes `-`. */
const NON_ID_CHARACTERS = /[^A-Za-z0-9_-]/g;

/** A budget field of a decision, as the patch and the expected state name it. */
type AmountField = 'daily_budget' | 'total_budget';

/** The patch fields read from stored JSON, in their fixed display order. */
const PATCH_FIELDS: readonly (keyof DecisionPatchView)[] = Object.freeze([
  'status',
  'daily_budget',
  'total_budget',
]);

/** The amount fields in display order. */
const AMOUNT_FIELDS: readonly AmountField[] = Object.freeze(['daily_budget', 'total_budget']);

/** How each field is named in the patch and expected-state cells. */
const FIELD_LABELS: Readonly<Record<keyof DecisionPatchView, string>> = Object.freeze({
  status: 'status',
  daily_budget: 'daily budget',
  total_budget: 'total budget',
});

// ---------------------------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------------------------

/** One subject's decisions, newest first. */
interface SubjectGroup {
  /** `${type}:${id}`, unique across subject types. */
  key: string;
  /** The subject as its newest decision names it. */
  subject: SubjectView;
  /** The subject's decisions, newest first; never empty. */
  decisions: Decision[];
}

/**
 * The subject the "Record decision" dialog is open for. Each opening builds a new one, because
 * its identity tells a save that ends late which opening it was made in.
 */
interface DialogTarget {
  /** The subject, as `DecisionForm` posts it. */
  subject: SubjectIn;
  /** The subject's account currency, so the form formats and validates amounts in it. */
  currency: string | null;
  /** The subject title, shown above the form so the dialog names what it records about. */
  title: string;
}

// ---------------------------------------------------------------------------------------------
// Subjects
// ---------------------------------------------------------------------------------------------

/** A trimmed, non-empty string, or `null` for anything else. */
function textOrNull(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** The grouping key of a subject: type and id, because ids are unique only within a type. */
function subjectKey(subject: SubjectView): string {
  return `${subject.type}:${subject.id}`;
}

/**
 * Groups decisions by subject. A `Map` keeps insertion order, so groups follow the first
 * appearance of each subject in the newest-first list, which is the order of their newest
 * decisions, and each group keeps the list's newest-first order.
 *
 * @param decisions - The decision log, newest first, as `GET /decisions` returns it.
 * @returns One group per subject.
 */
function groupBySubject(decisions: readonly Decision[]): SubjectGroup[] {
  const groups = new Map<string, SubjectGroup>();
  for (const decision of decisions) {
    const key = subjectKey(decision.subject);
    const group = groups.get(key);
    if (group === undefined) {
      groups.set(key, { key, subject: decision.subject, decisions: [decision] });
    } else {
      group.decisions.push(decision);
    }
  }
  return [...groups.values()];
}

/**
 * The anchor id of a subject's section, `decision-subject-{type}-{id}`, with every character
 * outside `[A-Za-z0-9_-]` replaced by `-` so it is a valid, selector-safe id.
 */
function subjectSectionId(subject: SubjectView): string {
  return `decision-subject-${subject.type}-${subject.id}`.replace(NON_ID_CHARACTERS, '-');
}

/** The label of a subject type; an unknown type received at run time is shown as sent. */
function subjectTypeLabel(type: SubjectType): string {
  return Object.hasOwn(SUBJECT_TYPE_LABELS, type) ? SUBJECT_TYPE_LABELS[type] : String(type);
}

/**
 * The display name of a subject: its name, or "Unnamed campaign (id 12)" / "Unnamed shared
 * budget (id 7)" when the API could not name it, so a caption never reads "null".
 */
function subjectName(subject: SubjectView): string {
  return (
    textOrNull(subject.name) ??
    `Unnamed ${subjectTypeLabel(subject.type).toLowerCase()} (id ${subject.id})`
  );
}

/** The section title of a subject: "{name} · {Campaign | Shared budget} · {source}". */
function subjectTitle(subject: SubjectView): string {
  const parts = [subjectName(subject), subjectTypeLabel(subject.type)];
  if (subject.source !== null) {
    parts.push(sourceLabel(subject.source));
  }
  return parts.join(PART_SEPARATOR);
}

/**
 * The names of the shared budgets in the log, by internal budget id, so a member's expected
 * state can name the budget it draws from rather than an internal id.
 */
function sharedBudgetNames(decisions: readonly Decision[]): ReadonlyMap<number, string> {
  const names = new Map<number, string>();
  for (const decision of decisions) {
    const name = textOrNull(decision.subject.name);
    if (decision.subject.type === 'budget' && name !== null && !names.has(decision.subject.id)) {
      names.set(decision.subject.id, name);
    }
  }
  return names;
}

/**
 * How a member's shared budget is named after "shared budget": the budget's name without the
 * API's own "Shared budget " prefix ("4455"), its full name when it has no such prefix, or
 * "id {budget_id}" when the log holds no decision naming it, the wording `DecisionForm` uses.
 */
function sharedBudgetLabel(budgetId: number, names: ReadonlyMap<number, string>): string {
  const name = names.get(budgetId);
  if (name === undefined) {
    return `id ${budgetId}`;
  }
  return textOrNull(name.replace(SHARED_BUDGET_NAME_PREFIX, '')) ?? name;
}

// ---------------------------------------------------------------------------------------------
// Row cells
// ---------------------------------------------------------------------------------------------

/** Whether a stored value is one of the normalised campaign statuses. */
function isCampaignStatus(value: unknown): value is CampaignStatus {
  return typeof value === 'string' && Object.hasOwn(CAMPAIGN_STATUS_LABELS, value);
}

/**
 * The label of a decided status ("Paused"), from `CAMPAIGN_STATUS_LABELS`, the labels
 * `DecisionForm` and `DiscrepancyResolver` use. A status outside the normalised set is shown as
 * stored, and a missing one reads "status not reported".
 */
function statusLabel(status: unknown): string {
  if (isCampaignStatus(status)) {
    return CAMPAIGN_STATUS_LABELS[status];
  }
  return textOrNull(status) ?? STATUS_NOT_REPORTED;
}

/**
 * The origin cell. A row recorded from a recommendation reads "Recommendation" whatever its
 * stored origin, with no link: run history is addressed by run, not by item. An origin added to
 * the API later is shown as sent.
 */
function originLabel(decision: Decision): string {
  if (decision.recommendation_id !== null) {
    return ORIGIN_LABELS.recommendation;
  }
  const { origin } = decision;
  return Object.hasOwn(ORIGIN_LABELS, origin) ? ORIGIN_LABELS[origin] : String(origin);
}

/**
 * One field of a stored patch, read defensively: the patch is stored `jsonb`, so the field is
 * read only when the patch is a plain object holding it as its own key with a non-blank string
 * value. Anything else counts as absent and is never cast.
 *
 * @param patch - The row's patch, as the API returned it.
 * @param field - A field `DecisionPatchView` defines.
 * @returns The trimmed value, or `null`.
 */
function patchValue(patch: unknown, field: keyof DecisionPatchView): string | null {
  if (typeof patch !== 'object' || patch === null || Array.isArray(patch)) {
    return null;
  }
  if (!Object.hasOwn(patch, field)) {
    return null;
  }
  return textOrNull(Reflect.get(patch, field));
}

/**
 * The "Patch applied" cell: the fields the submission carried, in the fixed order status,
 * daily budget, total budget, joined with "; ", for example "status: paused; daily budget:
 * €120.00". The status shows its stored value and amounts are formatted in the subject's
 * currency. An empty patch reads "Platform state at first sync" on a baseline row and an em dash
 * on any other row.
 *
 * A shared member's budget assignment is not a patch field: it changes only through a
 * discrepancy resolution, whose patch is empty (AAP 0.5.2), and `DecisionPatchView` defines no
 * `budget_id` key, so none is read here. The assignment shows in the expected state instead.
 */
function describePatch(decision: Decision): string {
  const currency = decision.subject.currency;
  const parts: string[] = [];
  for (const field of PATCH_FIELDS) {
    const value = patchValue(decision.patch, field);
    if (value === null) {
      continue;
    }
    const shown = field === 'status' ? value : formatMoney(value, currency);
    parts.push(`${FIELD_LABELS[field]}: ${shown}`);
  }
  if (parts.length > 0) {
    return parts.join(PATCH_SEPARATOR);
  }
  return decision.origin === 'baseline' ? BASELINE_PATCH_TEXT : EM_DASH;
}

/** The row's amounts that are set, as "daily budget $500.00", in the subject's currency. */
function amountParts(decision: Decision): string[] {
  const parts: string[] = [];
  for (const field of AMOUNT_FIELDS) {
    const amount = decision[field];
    if (amount !== null) {
      parts.push(`${FIELD_LABELS[field]} ${formatMoney(amount, decision.subject.currency)}`);
    }
  }
  return parts;
}

/**
 * The "Resulting expected state" cell: the complete state the row holds, by its decision shape
 * (AAP 0.5.2), the rule `DiscrepancyResolver` renders decided states with.
 *
 * - `own_budget`: "{Status} · daily budget {amount} · total budget {amount}", with only the
 *   amounts the row holds;
 * - `shared_member`: "{Status} · shared budget {name or id}", never an amount, because the
 *   amount belongs to the shared budget;
 * - `budget`: "daily budget {amount}" or "total budget {amount}", with no status;
 * - an acknowledged removal (`tombstone`): "Removal acknowledged", followed by the campaign's
 *   decided status (" · Ended") on campaign subjects.
 *
 * @param decision - The decision row.
 * @param budgetNames - Shared budget names by internal id, from {@link sharedBudgetNames}.
 * @returns The cell text.
 */
function describeExpectedState(
  decision: Decision,
  budgetNames: ReadonlyMap<number, string>,
): string {
  if (decision.tombstone) {
    return decision.subject.type === 'campaign'
      ? [REMOVAL_TEXT, statusLabel(decision.status ?? 'ended')].join(PART_SEPARATOR)
      : REMOVAL_TEXT;
  }
  switch (decision.shape) {
    case 'own_budget':
      return [statusLabel(decision.status), ...amountParts(decision)].join(PART_SEPARATOR);
    case 'shared_member': {
      const parts = [statusLabel(decision.status)];
      if (decision.budget_id !== null) {
        parts.push(`shared budget ${sharedBudgetLabel(decision.budget_id, budgetNames)}`);
      }
      return parts.join(PART_SEPARATOR);
    }
    case 'budget': {
      const parts = amountParts(decision);
      return parts.length === 0 ? NO_AMOUNT_TEXT : parts.join(PART_SEPARATOR);
    }
  }
}

/**
 * The columns of every subject's table, in their fixed order: Decided, Origin, Decided by,
 * Patch applied, Resulting expected state, Note. The decision instant identifies the row, so its
 * cells are the row headers.
 *
 * @param displayTimezone - The IANA zone instants are shown in.
 * @param budgetNames - Shared budget names by internal id, for members' expected states.
 * @returns The column definitions.
 */
function decisionColumns(
  displayTimezone: string,
  budgetNames: ReadonlyMap<number, string>,
): DataTableColumn<Decision>[] {
  return [
    {
      key: 'decided',
      header: 'Decided',
      isRowHeader: true,
      render: (decision) => formatInstant(decision.decided_at, displayTimezone),
    },
    { key: 'origin', header: 'Origin', render: originLabel },
    {
      key: 'decided_by',
      header: 'Decided by',
      render: (decision) => textOrNull(decision.decided_by) ?? SYSTEM_AUTHOR,
    },
    { key: 'patch', header: 'Patch applied', render: describePatch },
    {
      key: 'expected_state',
      header: 'Resulting expected state',
      render: (decision) => describeExpectedState(decision, budgetNames),
    },
    {
      key: 'note',
      header: 'Note',
      render: (decision) => textOrNull(decision.note) ?? EM_DASH,
    },
  ];
}

/** The React key and row key of a decision. */
function decisionRowKey(decision: Decision): string {
  return String(decision.id);
}

// ---------------------------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------------------------

/** Props of {@link SubjectDecisions}. */
interface SubjectDecisionsProps {
  /** The subject and its decisions, newest first. */
  group: SubjectGroup;
  /** The table columns, shared by every subject. */
  columns: readonly DataTableColumn<Decision>[];
  /** Opens the "Record decision" dialog for this subject. */
  onRecord: (target: DialogTarget) => void;
}

/**
 * One subject's section: an `<h3>` titled "{name} · {type} · {source}", a "Record decision"
 * action and the table captioned "Decisions for {name}". The action keeps the visible label as
 * its accessible name, which the e2e suite clicks, and is described by the section heading, so
 * a screen reader announces which subject it records for.
 */
function SubjectDecisions({ group, columns, onRecord }: SubjectDecisionsProps) {
  const { subject } = group;
  const sectionId = subjectSectionId(subject);
  const title = subjectTitle(subject);
  return (
    <PageShell.Section
      id={sectionId}
      title={title}
      headingLevel={3}
      actions={
        <Button
          variant="secondary"
          aria-describedby={`${sectionId}-title`}
          onClick={() => {
            onRecord({
              subject: { type: subject.type, id: subject.id },
              currency: textOrNull(subject.currency),
              title,
            });
          }}
        >
          Record decision
        </Button>
      }
    >
      <DataTable<Decision>
        caption={`Decisions for ${subjectName(subject)}`}
        columns={columns}
        rows={group.decisions}
        getRowKey={decisionRowKey}
      />
    </PageShell.Section>
  );
}

/**
 * The decision log of `/decisions`: every active-dataset decision, grouped by subject (campaign
 * or shared budget) and newest first, with when, by whom and from where each was recorded, the
 * patch it applied and the expected state it left (AAP 0.5.2, 0.5.5).
 *
 * `DecisionsPage` renders it inside its "Decision log" section, so each subject's heading is an
 * `<h3>`. It takes no props: the log comes from `GET /decisions` with no filter, and instants are
 * shown in the latest default run's `display_timezone` (`UTC` until a default run exists).
 *
 * States:
 * - loading: a `Spinner` "Loading decisions";
 * - failed with nothing loaded: an `EmptyState` "Decisions could not be loaded" with the API's
 *   message; a failed refetch keeps showing the log already loaded;
 * - empty: an `EmptyState` "No decisions recorded yet", because baseline decisions appear after
 *   the first entity sync;
 * - otherwise one section per subject, preceded by a note when the log reached the API's row
 *   limit and older decisions may be missing.
 *
 * "Record decision" on a subject opens one page-level `Dialog`, "Record decision", hosting
 * `DecisionForm` for that subject in its own currency. Saving or cancelling closes it, and the
 * form's save invalidates the log, so the new row appears at the top of its subject.
 */
export function DecisionLog() {
  const decisions = useDecisions();
  const defaultRun = useDefaultRun();
  const [dialogTarget, setDialogTarget] = useState<DialogTarget | null>(null);

  const displayTimezone = textOrNull(defaultRun.data?.display_timezone) ?? FALLBACK_TIMEZONE;
  const closeDialog = () => {
    setDialogTarget(null);
  };

  let content: ReactNode;
  if (decisions.data === undefined) {
    content =
      decisions.error === null ? (
        <Spinner label="Loading decisions" />
      ) : (
        <EmptyState title="Decisions could not be loaded">
          {textOrNull(decisions.error.message) ?? LOAD_ERROR_FALLBACK}
        </EmptyState>
      );
  } else if (decisions.data.length === 0) {
    content = (
      <EmptyState title="No decisions recorded yet">
        Baseline decisions appear after the first entity sync.
      </EmptyState>
    );
  } else {
    const rows = decisions.data;
    const columns = decisionColumns(displayTimezone, sharedBudgetNames(rows));
    content = (
      <Stack gap={5}>
        {/*
          BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive and feature code
          carries no CSS (AAP 0.4.4), so the limit note is a <div> spaced by the Stack gap token
          rather than a <p>, whose user-agent margins no spacing token sets.
        */}
        {rows.length >= DECISION_LOG_LIMIT ? (
          <div>
            {`Showing the newest ${DECISION_LOG_LIMIT} decisions; older decisions are not listed.`}
          </div>
        ) : null}
        {groupBySubject(rows).map((group) => (
          <SubjectDecisions
            key={group.key}
            group={group}
            columns={columns}
            onRecord={setDialogTarget}
          />
        ))}
      </Stack>
    );
  }

  return (
    <>
      {content}
      <Dialog open={dialogTarget !== null} title={DIALOG_TITLE} onClose={closeDialog}>
        {dialogTarget === null ? null : (
          <Stack gap={4}>
            <div>{dialogTarget.title}</div>
            <DecisionForm
              subject={dialogTarget.subject}
              currency={dialogTarget.currency}
              onDone={() => {
                // DecisionForm reports a save even after its dialog was closed mid-request, so
                // only the dialog of the target that saved closes. Every opening builds a fresh
                // target, so a dialog opened in the meantime, for another subject or reopened
                // for this one, keeps its edits.
                const savedTarget = dialogTarget;
                setDialogTarget((current) => (current === savedTarget ? null : current));
              }}
              onCancel={closeDialog}
            />
          </Stack>
        )}
      </Dialog>
    </>
  );
}
