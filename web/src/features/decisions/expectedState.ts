/**
 * The wording of a decision's expected state (AAP 0.5.2 decision shapes), shared by the decision
 * log on `/decisions` and the recommendation card on `/actions`, so both state a decision the
 * same way: {@link describeExpectedState} is the log's "Resulting expected state" cell, and the
 * card's "Decision recorded: {state}" line once its form has saved.
 *
 * It is a module of its own so the card, which `/actions` loads up front, does not pull
 * `DecisionLog.tsx` into the entry chunk: the decision log ships only with the lazily loaded
 * `/decisions` route (`router.tsx`). It therefore holds only that wording and the readers it
 * needs, which `DecisionLog.tsx` reuses for its other cells. Statuses read as the
 * `CAMPAIGN_STATUS_LABELS` labels `DecisionForm` exports, which the card already loads with the
 * form, and amounts are formatted by `formatMoney` in the subject's own currency.
 *
 * @example
 * ```ts
 * import { describeExpectedState } from '../decisions/expectedState';
 *
 * describeExpectedState(savedDecision); // "Active · daily budget USD 160.00"
 * ```
 *
 * @packageDocumentation
 */
import type { CampaignStatus, Decision, DecisionPatchView } from '../../api/types';
import { formatMoney } from '../../lib/format';
import { CAMPAIGN_STATUS_LABELS } from './DecisionForm';

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/** Separator between the parts of an expected state. */
export const PART_SEPARATOR = ' · ';

/** The expected state of a row recording an acknowledged removal (`tombstone`). */
const REMOVAL_TEXT = 'Removal acknowledged';

/** Shown when a row carries no status where its shape owns one. */
const STATUS_NOT_REPORTED = 'status not reported';

/** Shown when a shared budget's row carries neither amount. */
const NO_AMOUNT_TEXT = 'no amount recorded';

/**
 * A budget name as the API builds it for a shared budget subject ("Shared budget {external
 * id}", `_SHARED_BUDGET_NAME` in `api/routes/decisions.py`). Its prefix is dropped where the
 * text already says "shared budget", so a member reads "shared budget 4455", not
 * "shared budget Shared budget 4455".
 */
const SHARED_BUDGET_NAME_PREFIX = /^shared budget\s+/i;

/** A budget field of a decision, as the patch and the expected state name it. */
type AmountField = 'daily_budget' | 'total_budget';

/** The amount fields in display order. */
const AMOUNT_FIELDS: readonly AmountField[] = Object.freeze(['daily_budget', 'total_budget']);

/**
 * How each field is named in the decision log's patch cell and in an expected state: "status",
 * "daily budget" and "total budget".
 */
export const FIELD_LABELS: Readonly<Record<keyof DecisionPatchView, string>> = Object.freeze({
  status: 'status',
  daily_budget: 'daily budget',
  total_budget: 'total budget',
});

/** No shared budget names: a member's budget is then named by its id. */
const NO_BUDGET_NAMES: ReadonlyMap<number, string> = new Map<number, string>();

// ---------------------------------------------------------------------------------------------
// Readers
// ---------------------------------------------------------------------------------------------

/**
 * A trimmed, non-empty string, or `null` for anything else, so stored or API text is shown only
 * when it holds something.
 *
 * @param value - Any value, as stored JSON or the API may hold it.
 * @returns The trimmed string, or `null` for a non-string or a blank string.
 *
 * @example
 * ```ts
 * textOrNull('  Paused '); // "Paused"
 * textOrNull('  '); // null
 * ```
 */
export function textOrNull(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** Whether a stored value is one of the normalised campaign statuses. */
function isCampaignStatus(value: unknown): value is CampaignStatus {
  return typeof value === 'string' && Object.hasOwn(CAMPAIGN_STATUS_LABELS, value);
}

/**
 * The label of a decided status ("Paused"), from `CAMPAIGN_STATUS_LABELS`, the labels
 * `DecisionForm` and `DiscrepancyResolver` use. A status outside the normalised set is shown as
 * stored, and a missing one reads "status not reported".
 *
 * @param status - The status a decision row or its stored patch holds.
 * @returns The label.
 *
 * @example
 * ```ts
 * statusLabel('paused'); // "Paused"
 * statusLabel(null); // "status not reported"
 * ```
 */
export function statusLabel(status: unknown): string {
  if (isCampaignStatus(status)) {
    return CAMPAIGN_STATUS_LABELS[status];
  }
  return textOrNull(status) ?? STATUS_NOT_REPORTED;
}

/**
 * How a member's shared budget is named after "shared budget", the wording `DecisionForm` uses:
 * the budget's name from a decision of the log, without the API's own "Shared budget " prefix
 * ("4455"), or its full name when it has no such prefix; else the Google id the member's row
 * carries (`budget_external_id`); else "id {budget_id}" when the API could not name it either.
 *
 * @param budgetId - The member's `budget_id`.
 * @param externalId - The member row's `budget_external_id`.
 * @param names - Shared budget names by internal id, as the decision log collects them.
 * @returns The label.
 */
function sharedBudgetLabel(
  budgetId: number,
  externalId: string | null,
  names: ReadonlyMap<number, string>,
): string {
  const name = names.get(budgetId);
  if (name !== undefined) {
    return textOrNull(name.replace(SHARED_BUDGET_NAME_PREFIX, '')) ?? name;
  }
  return textOrNull(externalId) ?? `id ${budgetId}`;
}

/** The row's amounts that are set, as "daily budget USD 500.00", in the subject's currency. */
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

// ---------------------------------------------------------------------------------------------
// Expected state
// ---------------------------------------------------------------------------------------------

/**
 * The complete expected state a decision row holds, by its decision shape (AAP 0.5.2), the rule
 * `DiscrepancyResolver` renders decided states with. It is the decision log's "Resulting
 * expected state" cell, and the recommendation card states a decision it has just recorded with
 * it, from the `POST /decisions` response, so both read the same.
 *
 * - `own_budget`: "{Status} · daily budget {amount} · total budget {amount}", with only the
 *   amounts the row holds;
 * - `shared_member`: "{Status} · shared budget {name, Google id or id}" (see
 *   {@link sharedBudgetLabel}), never an amount, because the amount belongs to the shared budget;
 * - `budget`: "daily budget {amount}" or "total budget {amount}", with no status;
 * - an acknowledged removal (`tombstone`): "Removal acknowledged", followed by the campaign's
 *   decided status (" · Ended") on campaign subjects.
 *
 * Statuses read as `CAMPAIGN_STATUS_LABELS` labels, and amounts are formatted by `formatMoney`
 * in the subject's own currency.
 *
 * @param decision - The decision row.
 * @param budgetNames - Shared budget names by internal id, which the decision log collects from
 *   the shared budget subjects it lists. Omitted, a member's shared budget is named by its
 *   Google id, else by its internal id ("shared budget id 401").
 * @returns The state, as one line.
 *
 * @example
 * ```ts
 * describeExpectedState(savedDecision); // "Active · daily budget USD 160.00"
 * ```
 */
export function describeExpectedState(
  decision: Decision,
  budgetNames: ReadonlyMap<number, string> = NO_BUDGET_NAMES,
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
        const label = sharedBudgetLabel(decision.budget_id, decision.budget_external_id, budgetNames);
        parts.push(`shared budget ${label}`);
      }
      return parts.join(PART_SEPARATOR);
    }
    case 'budget': {
      const parts = amountParts(decision);
      return parts.length === 0 ? NO_AMOUNT_TEXT : parts.join(PART_SEPARATOR);
    }
  }
}
