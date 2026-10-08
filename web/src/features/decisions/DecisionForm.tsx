/**
 * The decision form: records a decision on a campaign or a Google shared budget as a patch
 * (AAP 0.5.2, 0.5.3 `POST /decisions`), and records the intended state of an `untracked`
 * discrepancy (AAP 0.5.4.4 `record_intended`, `POST /discrepancies/{id}/resolve`).
 *
 * It opens from three places: the "Record decision" footer action of a recommendation card
 * (`features/actions/RecommendationCard.tsx`, prefilled with the item's proposed change), the
 * "Record decision" action of a subject on `/decisions` (`DecisionLog.tsx`) and the "Record
 * intended state" resolution of a C0 discrepancy (`DiscrepancyResolver.tsx`).
 *
 * Rules this file keeps:
 *
 * - **Shape decides the fields.** The subject's decision shape comes from the API (the latest
 *   decision, or the discrepancy), never from its name or source. `own_budget` offers status
 *   plus exactly the budget fields the subject has; `shared_member` offers status only, because
 *   the amount belongs to its shared budget and its assignment changes only through a
 *   discrepancy resolution; `budget` offers its one amount and no status (AAP 0.5.2). A prefill
 *   field the shape does not own is dropped and never posted.
 * - **Patches carry only changes.** In decision mode the posted patch holds only the fields
 *   whose value differs from the last decided state; the server materialises the rest. An
 *   unchanged form cannot be submitted, because the server refuses an empty or no-change patch
 *   with 422. A prefill that differs from the decided state is a change, so it is posted.
 * - **Intended states are complete.** In intended-state mode every field of the shape is sent,
 *   because `record_intended` requires the complete state (AAP 0.5.4.4).
 * - **Money stays a decimal string.** Amounts are never converted to numbers: they are
 *   validated with patterns and compared as normalised decimal strings, so `"120"`, `"120.0"`
 *   and `"120.00"` are equal and no binary float ever touches an amount.
 * - **Errors are the API's.** 422 messages are shown as returned. A 409 `subject_retired`
 *   locks the form for the rest of its mount (AAP 0.3.2), and a 409 `discrepancy_resolved`
 *   reads "Already resolved". Error banners never take `role="alert"`, which AAP 0.5.5
 *   reserves for the discrepancy banner on `/actions`.
 *
 * Server state goes only through `../../api/queries` (AAP 0.11.1); the mutations there already
 * invalidate the recommendations, discrepancies and decision log, so nothing is invalidated
 * here. UI comes only from the design-system index (AAP 0.4.4).
 *
 * @example
 * ```tsx
 * <Dialog open title="Record decision" onClose={close}>
 *   <DecisionForm
 *     subject={item.decision_subject}
 *     recommendationId={item.id}
 *     prefill={{ status: 'paused' }}
 *     currency="USD"
 *     onDone={close}
 *     onCancel={close}
 *   />
 * </Dialog>
 * ```
 *
 * @packageDocumentation
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';

import {
  isApiError,
  useDecisions,
  useRecordDecision,
  useResolveDiscrepancy,
} from '../../api/queries';
import type {
  CampaignStatus,
  Decision,
  DecisionPatchIn,
  DecisionRequest,
  DecisionShape,
  Discrepancy,
  DiscrepancyValue,
  IntendedStateIn,
  Money,
  RecommendationId,
  ResolveRequest,
  SubjectIn,
} from '../../api/types';
import {
  Banner,
  Button,
  Inline,
  Select,
  Spinner,
  Stack,
  TextField,
  type SelectOption,
} from '../../design-system';
import { currencyMinorDigits, currencyStep, formatMoney } from '../../lib/format';

// ---------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------

/**
 * What the form records:
 *
 * - `decision` (the default): a patch on the subject through `POST /decisions`;
 * - `intended_state`: the C0 "Record intended state" resolution of an `untracked` discrepancy
 *   through `POST /discrepancies/{id}/resolve` with `resolution: 'record_intended'`.
 */
export type DecisionFormMode = 'decision' | 'intended_state';

/**
 * Props of {@link DecisionForm}.
 *
 * `subject` and `recommendationId` carry the API's own id types (`SubjectIn`, `Item['id']`), so
 * a recommendation's `decision_subject` and `id` pass through unchanged.
 */
export interface DecisionFormProps {
  /** The campaign, or Google explicitly shared budget, the decision is about. */
  subject: SubjectIn;
  /** What the form records. Defaults to `'decision'`. */
  mode?: DecisionFormMode;
  /**
   * The discrepancy being resolved. Required when `mode` is `'intended_state'`: it names the
   * discrepancy to resolve and carries the subject's shape and current platform state. In
   * decision mode it is optional and only used when the subject has no decision yet.
   */
  discrepancy?: Discrepancy;
  /**
   * The proposed change from a recommendation, as a patch: status `paused` for C1 and C1b, the
   * proposed daily budget for B1, B2, B3 and C2 "Scale" (AAP 0.5.5). Fields the subject's
   * shape does not own are dropped.
   */
  prefill?: Partial<DecisionPatchIn>;
  /** The item the decision is recorded from; posted as `recommendation_id` when given. */
  recommendationId?: RecommendationId;
  /** The member campaigns of a shared budget, listed on the form of a `budget` subject. */
  members?: readonly { id: string; name: string }[];
  /**
   * The currency the subject's amounts are in. Omitted or blank, the latest decision's or the
   * discrepancy's subject currency is used.
   */
  currency?: string | null;
  /** Called after the decision or intended state has been saved. */
  onDone?: () => void;
  /** Called by the "Cancel" button, which is rendered only when this is given. */
  onCancel?: () => void;
  /** Disables every field and the submit button, for example on a historical item. */
  disabled?: boolean;
}

/**
 * The display label of each normalised campaign status. `DecisionLog.tsx` and
 * `DiscrepancyResolver.tsx` render decided and platform states with the same labels.
 */
export const CAMPAIGN_STATUS_LABELS: Readonly<Record<CampaignStatus, string>> = Object.freeze({
  active: 'Active',
  paused: 'Paused',
  ended: 'Ended',
});

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/** The statuses offered, in display order. */
const CAMPAIGN_STATUSES: readonly CampaignStatus[] = Object.freeze(['active', 'paused', 'ended']);

/** The longest note the API accepts (`schemas.NOTE_MAX_LENGTH`), in characters. */
const NOTE_MAX_LENGTH = 2000;

/** Shown when a write is refused because the subject's dataset was replaced (AAP 0.3.2). */
const RETIRED_MESSAGE = 'Computed from replaced data';

/** Shown when the discrepancy was resolved before this resolution reached the server. */
const RESOLVED_MESSAGE = 'Already resolved';

/** Shown when the subject has neither a decision nor a discrepancy to start from. */
const NO_STATE_MESSAGE = 'No decided state is recorded for this subject yet.';

/** Shown under the submit button while the patch would change nothing. */
const NO_CHANGE_MESSAGE = 'No change from the last decided state';

/** An amount as the form accepts it: a non-negative plain decimal, such as `120` or `120.50`. */
const AMOUNT_PATTERN = /^\d+(\.\d+)?$/;

/**
 * A plain decimal as the normaliser reads it. A trailing dot (`"120."`) is accepted here, so
 * it compares equal to `"120"`; validation still refuses it.
 */
const NORMALISABLE_PATTERN = /^(\d+)(?:\.(\d*))?$/;

// ---------------------------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------------------------

/** A budget field of an expected state. */
type AmountField = 'daily_budget' | 'total_budget';

/** The amount fields in display order. */
const AMOUNT_FIELDS: readonly AmountField[] = Object.freeze(['daily_budget', 'total_budget']);

/** The label of each amount field. */
const AMOUNT_LABELS: Readonly<Record<AmountField, string>> = Object.freeze({
  daily_budget: 'Daily budget',
  total_budget: 'Total budget',
});

/** The status the select holds: a campaign status, or `''` while none is chosen. */
type StatusChoice = CampaignStatus | '';

/** The expected state the form starts from and compares against. */
interface BaseState {
  /** The status; `null` for a budget subject or when the platform reported none. */
  status: CampaignStatus | null;
  /** The daily budget; `null` when the subject has none. */
  daily_budget: Money | null;
  /** The lifetime or custom-period budget; `null` when the subject has none. */
  total_budget: Money | null;
  /** How a shared member's budget is named on the form; `null` when unknown. */
  sharedBudgetLabel: string | null;
}

/** The subject's shape and the state the form starts from. */
interface SubjectState {
  /** The decision shape, which fixes the fields offered. */
  shape: DecisionShape;
  /** The state the fields start from and changes are measured against. */
  base: BaseState;
}

/** The subject's fields, as the shape owns them. */
interface ShapeFields {
  /** Whether the shape owns a status. */
  status: boolean;
  /** The amount fields the subject has, in display order. */
  amounts: readonly AmountField[];
}

// ---------------------------------------------------------------------------------------------
// Decimal helpers (strings only; money never becomes a number)
// ---------------------------------------------------------------------------------------------

/**
 * A decimal string in canonical form, for comparison: trimmed, without leading zeros in the
 * integer part, without trailing fractional zeros and without a trailing dot. `"120"`,
 * `"120.0"`, `"0120.00"` and `"120."` all give `"120"`; `"0.50"` gives `"0.5"`. A string that
 * is not a plain decimal is returned trimmed, so it never equals a valid amount.
 *
 * @param value - The amount as typed or as the API sent it.
 * @returns The canonical text.
 */
function normaliseDecimal(value: string): string {
  const trimmed = value.trim();
  const match = NORMALISABLE_PATTERN.exec(trimmed);
  if (match === null) {
    return trimmed;
  }
  const integer = (match[1] ?? '').replace(/^0+(?=\d)/, '');
  const fraction = (match[2] ?? '').replace(/0+$/, '');
  return fraction === '' ? integer : `${integer}.${fraction}`;
}

/** The number of digits after the decimal point of a trimmed decimal string. */
function fractionDigits(value: string): number {
  const dot = value.indexOf('.');
  return dot === -1 ? 0 : value.length - dot - 1;
}

/**
 * An API amount as the input shows it: canonical, then padded to the currency's minor unit,
 * so `"100.000000"` in EUR reads `"100.00"` and a value typed back unchanged compares equal.
 * An amount finer than the minor unit keeps its digits rather than being rounded.
 *
 * @param value - The amount, as the API or a prefill sent it.
 * @param digits - The currency's minor-unit digits.
 * @returns The text for the input.
 */
function toInputAmount(value: Money, digits: number): string {
  const canonical = normaliseDecimal(value);
  if (!AMOUNT_PATTERN.test(canonical)) {
    return value.trim();
  }
  const dot = canonical.indexOf('.');
  const integer = dot === -1 ? canonical : canonical.slice(0, dot);
  const fraction = dot === -1 ? '' : canonical.slice(dot + 1);
  if (fraction.length > digits) {
    return canonical;
  }
  return digits === 0 ? integer : `${integer}.${fraction.padEnd(digits, '0')}`;
}

/**
 * Why an amount cannot be submitted, or `null` when it can. The server's own 422 rules
 * (AAP 0.5.2) still apply on top of these.
 *
 * The API requires a positive amount (`invalid_amount`), so zero is refused here as well, with
 * its own message, beyond the non-negative pattern.
 *
 * @param value - The amount as typed.
 * @param digits - The currency's minor-unit digits.
 * @returns The message for the field's `error`, or `null`.
 */
function amountError(value: string, digits: number): string | null {
  const trimmed = value.trim();
  if (trimmed === '') {
    return 'Enter an amount';
  }
  if (!AMOUNT_PATTERN.test(trimmed)) {
    return 'Enter a non-negative amount';
  }
  if (fractionDigits(trimmed) > digits) {
    return digits === 0
      ? 'Use a whole amount, with no decimal places'
      : `Use at most ${digits} decimal ${digits === 1 ? 'place' : 'places'}`;
  }
  if (normaliseDecimal(trimmed) === '0') {
    return 'Enter an amount above zero';
  }
  return null;
}

/** A trimmed, non-empty string, or `null`. */
function textOrNull(value: string | null | undefined): string | null {
  if (value === undefined || value === null) {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** Why a note cannot be submitted, or `null`. The length is counted in characters. */
function noteError(note: string): string | null {
  return Array.from(note).length > NOTE_MAX_LENGTH
    ? `Use at most ${NOTE_MAX_LENGTH.toLocaleString('en')} characters`
    : null;
}

// ---------------------------------------------------------------------------------------------
// Subject state
// ---------------------------------------------------------------------------------------------

/** The subject state a decision row records: its shape and complete expected state. */
function stateFromDecision(decision: Decision): SubjectState {
  return {
    shape: decision.shape,
    base: {
      status: decision.status,
      daily_budget: decision.daily_budget,
      total_budget: decision.total_budget,
      sharedBudgetLabel: decision.budget_id === null ? null : `id ${decision.budget_id}`,
    },
  };
}

/**
 * The subject state a discrepancy carries: its shape and the platform side, which for an
 * `untracked` discrepancy is the subject's complete current state. A discrepancy without a
 * platform side (`missing`) falls back to its decided side.
 *
 * @returns `null` when the discrepancy carries neither side.
 */
function stateFromDiscrepancy(discrepancy: Discrepancy): SubjectState | null {
  const value: DiscrepancyValue | null = discrepancy.platform_value ?? discrepancy.decided_value;
  if (value === null) {
    return null;
  }
  const externalId = textOrNull(value.budget_external_id);
  const budgetId = value.budget_id ?? null;
  return {
    shape: discrepancy.shape,
    base: {
      status: value.status ?? null,
      daily_budget: value.daily_budget ?? null,
      total_budget: value.total_budget ?? null,
      sharedBudgetLabel: externalId ?? (budgetId === null ? null : `id ${budgetId}`),
    },
  };
}

/**
 * The state the form starts from.
 *
 * - Decision mode: the latest decision, the subject's current expected state (AAP 0.5.2), else
 *   the discrepancy's platform state (a subject with no decision yet).
 * - Intended-state mode: the discrepancy first, because `record_intended` is validated against
 *   the subject's current shape and budget fields, which the discrepancy carries
 *   (`Discrepancy.shape` fixes the intended-state fields), and the latest decision only when the
 *   discrepancy has no state to offer.
 *
 * @returns `null` when neither is available.
 */
function resolveSubjectState(
  mode: DecisionFormMode,
  latest: Decision | undefined,
  discrepancy: Discrepancy | undefined,
): SubjectState | null {
  const fromDecision = latest === undefined ? null : stateFromDecision(latest);
  const fromDiscrepancy = discrepancy === undefined ? null : stateFromDiscrepancy(discrepancy);
  return mode === 'intended_state'
    ? (fromDiscrepancy ?? fromDecision)
    : (fromDecision ?? fromDiscrepancy);
}

/**
 * The fields a shape owns for this subject (AAP 0.5.2): `own_budget` status plus each budget
 * field the subject has, `shared_member` status only, `budget` its one amount (the daily
 * budget of a `DAILY` budget, the total of a `CUSTOM_PERIOD` one) and no status.
 */
function shapeFields(state: SubjectState): ShapeFields {
  const { shape, base } = state;
  switch (shape) {
    case 'own_budget':
      return {
        status: true,
        amounts: AMOUNT_FIELDS.filter((field) => base[field] !== null),
      };
    case 'shared_member':
      return { status: true, amounts: [] };
    case 'budget': {
      const owned = AMOUNT_FIELDS.find((field) => base[field] !== null);
      return { status: false, amounts: owned === undefined ? [] : [owned] };
    }
  }
}

/**
 * A state as one line, for the read-only "Last decided" summary: the status label, then each
 * amount in the subject's currency, or the shared budget a member draws from.
 */
function describeState(state: SubjectState, currency: string | null): string {
  const { shape, base } = state;
  const parts: string[] = [];
  if (shape !== 'budget') {
    parts.push(base.status === null ? 'status not reported' : CAMPAIGN_STATUS_LABELS[base.status]);
  }
  if (shape === 'shared_member') {
    if (base.sharedBudgetLabel !== null) {
      parts.push(`shared budget ${base.sharedBudgetLabel}`);
    }
  } else {
    if (base.daily_budget !== null) {
      parts.push(`daily budget ${formatMoney(base.daily_budget, currency)}`);
    }
    if (base.total_budget !== null) {
      parts.push(`total budget ${formatMoney(base.total_budget, currency)}`);
    }
  }
  return parts.length === 0 ? 'no amount recorded' : parts.join(' · ');
}

/**
 * The text of a failed save: "Computed from replaced data" for a 409 `subject_retired`
 * (AAP 0.3.2), "Already resolved" for a 409 `discrepancy_resolved`, otherwise the API's own
 * message, which for a 422 names the patch rule it broke.
 *
 * The backend names a closed discrepancy `discrepancy_resolved`; it sends no
 * `discrepancy_closed`, so that is the code read here.
 */
function saveErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    if (error.status === 409 && error.code === 'subject_retired') {
      return RETIRED_MESSAGE;
    }
    if (error.status === 409 && error.code === 'discrepancy_resolved') {
      return RESOLVED_MESSAGE;
    }
    return error.message;
  }
  return error instanceof Error && error.message !== '' ? error.message : 'The save failed.';
}

/** Whether a failed save means the subject's dataset was replaced (409 `subject_retired`). */
function isRetiredError(error: unknown): boolean {
  return isApiError(error) && error.status === 409 && error.code === 'subject_retired';
}

/** Whether a failed save means the discrepancy is already closed (409 `discrepancy_resolved`). */
function isResolvedError(error: unknown): boolean {
  return isApiError(error) && error.status === 409 && error.code === 'discrepancy_resolved';
}

// ---------------------------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------------------------

/**
 * Records a decision on a campaign or shared budget as a patch, or, with
 * `mode="intended_state"`, the intended state of an `untracked` discrepancy.
 *
 * It loads the subject's decision log (`GET /decisions?subject_type=&subject_id=`, newest
 * first), takes the latest row as the last decided state, and offers exactly the fields the
 * subject's decision shape owns, starting from that state overlaid with the shape-allowed
 * `prefill` fields:
 *
 * - `own_budget`: "Status", plus "Daily budget" and/or "Total budget" where the subject has one;
 * - `shared_member`: "Status" only, noting the shared budget that owns the amount;
 * - `budget`: its one amount ("Daily budget" or "Total budget") and no status, listing the
 *   `members` that draw from it.
 *
 * Decision mode posts `{ subject, patch, recommendation_id?, note? }` with only the changed
 * fields and is disabled while nothing changed. Intended-state mode posts
 * `{ resolution: 'record_intended', intended_state, note? }` with the complete state for the
 * shape. Success calls `onDone`. A 409 `subject_retired` disables every control for the rest of
 * the mount; the enclosing `Dialog` keeps its own Close button.
 *
 * States: a `Spinner` while the decision log loads; a critical `Banner` with the API message
 * when it fails; a warning `Banner` with no submit when the subject has no decided state and no
 * discrepancy to start from, or when its removal was acknowledged.
 */
export function DecisionForm(props: DecisionFormProps) {
  const {
    subject,
    mode = 'decision',
    discrepancy,
    prefill,
    recommendationId,
    members,
    onDone,
    onCancel,
    disabled = false,
  } = props;

  const decisions = useDecisions({ subject_type: subject.type, subject_id: subject.id });
  const recordDecision = useRecordDecision();
  const resolveDiscrepancy = useResolveDiscrepancy();

  // Only the user's edits are state; untouched fields follow the base state and the prefill,
  // so a refetched decision log after a save moves them along without a remount.
  const [statusDraft, setStatusDraft] = useState<StatusChoice | undefined>(undefined);
  const [amountDrafts, setAmountDrafts] = useState<Partial<Record<AmountField, string>>>({});
  const [note, setNote] = useState('');
  // A 409 subject_retired is final for this mount (AAP 0.3.2), and so is a closed discrepancy.
  const [retired, setRetired] = useState(false);
  const [closed, setClosed] = useState(false);
  const submitHintId = useId();

  // The submit button is disabled while its request runs, which drops keyboard focus to the
  // page. When a save fails, focus moves to the error instead, so keyboard and screen-reader
  // users land on the message (WCAG 2.2 SC 2.4.3, 3.3.1) without a role="alert" live region,
  // which AAP 0.5.5 reserves for the discrepancy banner on /actions.
  const activeMutation = mode === 'decision' ? recordDecision : resolveDiscrepancy;
  const saveError = activeMutation.error;
  const errorRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (saveError !== null) {
      errorRef.current?.focus();
    }
  }, [saveError]);

  if (decisions.data === undefined) {
    if (decisions.isError) {
      return <Banner tone="critical" title={saveErrorMessage(decisions.error)} />;
    }
    return <Spinner label="Loading decision" />;
  }

  if (mode === 'intended_state' && discrepancy === undefined) {
    return (
      <Banner tone="critical" title="No discrepancy was given to record an intended state for." />
    );
  }

  const latest = decisions.data[0];
  const state = resolveSubjectState(mode, latest, discrepancy);
  if (state === null) {
    return <Banner tone="warning" title={NO_STATE_MESSAGE} />;
  }
  if (mode === 'decision' && latest?.tombstone === true) {
    // The API refuses every patch on a tombstoned subject (422 subject_removed).
    const name = textOrNull(latest.subject.name) ?? 'This subject';
    return (
      <Banner tone="warning" title={`${name} was acknowledged as removed`}>
        It takes no further decisions.
      </Banner>
    );
  }

  const fields = shapeFields(state);
  if (!fields.status && fields.amounts.length === 0) {
    return <Banner tone="warning" title="No budget amount is known for this shared budget yet." />;
  }

  const currency =
    textOrNull(props.currency) ??
    textOrNull(latest?.subject.currency) ??
    textOrNull(discrepancy?.subject.currency) ??
    textOrNull(discrepancy?.platform_value?.currency) ??
    textOrNull(discrepancy?.decided_value?.currency);
  const digits = currencyMinorDigits(currency);
  const { base } = state;

  // Initial values: the base state overlaid with the prefill fields the shape owns. A prefill
  // field outside the shape is never read, so it can never be posted.
  const baseStatus: StatusChoice = base.status ?? '';
  const initialStatus: StatusChoice =
    fields.status && prefill?.status !== undefined ? prefill.status : baseStatus;
  const initialAmount = (field: AmountField): string => {
    const proposed = prefill?.[field];
    const value = proposed ?? base[field];
    return value === null ? '' : toInputAmount(value, digits);
  };

  const statusValue: StatusChoice = statusDraft ?? initialStatus;
  const amountValue = (field: AmountField): string => amountDrafts[field] ?? initialAmount(field);

  const statusChanged = fields.status && statusValue !== baseStatus;
  const amountChanged = (field: AmountField): boolean =>
    normaliseDecimal(amountValue(field)) !== normaliseDecimal(base[field] ?? '');

  // A field is validated when it would be posted: every field of an intended state, and only
  // the changed fields of a patch (unchanged ones are not sent).
  const isPosted = (field: AmountField): boolean =>
    mode === 'intended_state' || amountChanged(field);
  const amountErrors: Partial<Record<AmountField, string>> = {};
  for (const field of fields.amounts) {
    const issue = isPosted(field) ? amountError(amountValue(field), digits) : null;
    if (issue !== null) {
      amountErrors[field] = issue;
    }
  }
  const statusError =
    fields.status && mode === 'intended_state' && statusValue === '' ? 'Choose a status' : null;
  const noteIssue = noteError(note);
  const hasErrors =
    Object.keys(amountErrors).length > 0 || statusError !== null || noteIssue !== null;

  const patch: DecisionPatchIn = {};
  if (statusChanged && statusValue !== '') {
    patch.status = statusValue;
  }
  for (const field of fields.amounts) {
    if (amountChanged(field) && amountErrors[field] === undefined) {
      patch[field] = amountValue(field).trim();
    }
  }
  const patchIsEmpty = Object.keys(patch).length === 0;

  const intendedState: IntendedStateIn = {};
  if (fields.status && statusValue !== '') {
    intendedState.status = statusValue;
  }
  for (const field of fields.amounts) {
    if (amountErrors[field] === undefined) {
      intendedState[field] = amountValue(field).trim();
    }
  }

  const locked = disabled || retired;
  const pending = activeMutation.isPending;
  const canSubmit =
    !locked &&
    !hasErrors &&
    (mode === 'decision' ? !patchIsEmpty : !closed) &&
    !pending;
  const showNoChange = mode === 'decision' && patchIsEmpty && !hasErrors && !locked;

  const noteText = note.trim();

  const handleError = (error: unknown) => {
    if (isRetiredError(error)) {
      setRetired(true);
    } else if (isResolvedError(error)) {
      setClosed(true);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    if (mode === 'decision') {
      const body: DecisionRequest = { subject: { type: subject.type, id: subject.id }, patch };
      if (recommendationId !== undefined) {
        body.recommendation_id = recommendationId;
      }
      if (noteText !== '') {
        body.note = noteText;
      }
      recordDecision.mutate(body, { onSuccess: () => onDone?.(), onError: handleError });
      return;
    }
    if (discrepancy === undefined) {
      return;
    }
    const body: ResolveRequest = { resolution: 'record_intended', intended_state: intendedState };
    if (noteText !== '') {
      body.note = noteText;
    }
    resolveDiscrepancy.mutate(
      { id: discrepancy.id, body },
      { onSuccess: () => onDone?.(), onError: handleError },
    );
  };

  const statusOptions: SelectOption<StatusChoice>[] = CAMPAIGN_STATUSES.map((status) => ({
    value: status,
    label: CAMPAIGN_STATUS_LABELS[status],
  }));
  if (statusValue === '') {
    // The platform reported no status: an explicit, unselectable prompt keeps the select's
    // shown value equal to its state until a status is chosen.
    statusOptions.unshift({ value: '', label: 'Choose a status', disabled: true });
  }

  const summary =
    latest === undefined
      ? `No decision recorded yet · platform shows ${describeState(state, currency)}`
      : `Last decided: ${describeState(stateFromDecision(latest), currency)}`;
  const memberNames = (members ?? []).map((member) => member.name).filter((name) => name !== '');
  const saveLabel = mode === 'decision' ? 'Save decision' : 'Save intended state';
  const errorText = retired
    ? RETIRED_MESSAGE
    : saveError === null
      ? null
      : saveErrorMessage(saveError);

  return (
    <form noValidate onSubmit={handleSubmit}>
      {/*
        BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive, and feature code
        carries no CSS (AAP 0.4.4). Text lines are therefore <div> elements placed directly in the
        Stack rather than <p>: the base layer keeps the user-agent paragraph margins, which would
        add space no spacing token sets, so the Stack gap tokens alone space every line. The
        error's wrapper is unstyled and exists only as the focus target after a failed save.
      */}
      <Stack gap={4}>
        {errorText === null ? null : (
          <div ref={errorRef} tabIndex={-1}>
            <Banner tone="critical" title={errorText} />
          </div>
        )}
        <div>{summary}</div>
        {state.shape === 'shared_member' ? (
          <div>
            {base.sharedBudgetLabel === null
              ? 'The amount is set on its shared budget'
              : `The amount is set on shared budget ${base.sharedBudgetLabel}`}
          </div>
        ) : null}
        {state.shape === 'budget' && memberNames.length > 0 ? (
          <div>{`Applies to the shared budget used by: ${memberNames.join(', ')}`}</div>
        ) : null}
        {fields.status ? (
          <Select<StatusChoice>
            label="Status"
            name="status"
            options={statusOptions}
            value={statusValue}
            onChange={setStatusDraft}
            error={statusError}
            disabled={locked}
          />
        ) : null}
        {fields.amounts.map((field) => (
          <TextField
            key={field}
            label={AMOUNT_LABELS[field]}
            name={field}
            type="number"
            inputMode="decimal"
            min="0"
            step={currencyStep(currency)}
            hint={currency === null ? 'In the account currency' : `In ${currency}`}
            error={amountErrors[field]}
            value={amountValue(field)}
            onChange={(value) => {
              setAmountDrafts((drafts) => ({ ...drafts, [field]: value }));
            }}
            disabled={locked}
          />
        ))}
        <TextField
          multiline
          label="Note"
          name="note"
          hint="Optional"
          error={noteIssue}
          value={note}
          onChange={setNote}
          disabled={locked}
        />
        <Stack gap={2}>
          <Inline gap={2}>
            <Button
              type="submit"
              variant="primary"
              loading={pending}
              disabled={!canSubmit}
              aria-describedby={showNoChange ? submitHintId : undefined}
            >
              {saveLabel}
            </Button>
            {onCancel === undefined ? null : (
              <Button variant="secondary" onClick={onCancel} disabled={retired}>
                Cancel
              </Button>
            )}
          </Inline>
          {showNoChange ? <div id={submitHintId}>{NO_CHANGE_MESSAGE}</div> : null}
          {activeMutation.isSuccess ? (
            // A polite status message (WCAG 2.2 SC 4.1.3); role="alert" stays reserved for the
            // discrepancy banner on /actions (AAP 0.5.5).
            <div role="status">
              {mode === 'decision' ? 'Decision saved' : 'Intended state saved'}
            </div>
          ) : null}
        </Stack>
      </Stack>
    </form>
  );
}
