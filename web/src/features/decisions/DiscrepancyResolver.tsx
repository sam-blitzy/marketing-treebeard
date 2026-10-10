/**
 * The C0 discrepancy resolution controls (AAP 0.5.4.4, 0.5.5).
 *
 * Two components share one label map:
 *
 * - {@link DiscrepancyResolution}: the controls of one discrepancy. `/decisions` renders it in
 *   every resolver card, and `features/actions/sections/CampaignsSection.tsx` renders it inline
 *   on C0 cards, disabled for historical items.
 * - {@link DiscrepancyResolver}: the `/decisions` list of open and awaiting-platform
 *   discrepancies, oldest first, one card each, a page at a time with "Show more" (AAP 0.5.5
 *   "`DiscrepancyResolver` lists open and awaiting-platform discrepancies with the resolutions
 *   valid for each kind and subject type"). Its load, empty, refresh and closure outcomes are
 *   announced through one persistent polite status region. A link to
 *   `/decisions#discrepancy-{id}` loads pages until that card is shown, then scrolls it into
 *   view, or says the discrepancy is no longer unresolved.
 *
 * Rules this file keeps:
 *
 * - **The API decides the resolutions.** The buttons are exactly `Discrepancy.resolutions`, in
 *   the order the API lists them. The API computes them from the discrepancy's kind and the
 *   subject's decision shape (`RESOLUTIONS[(kind, shape)]`), so nothing here adds, drops,
 *   reorders or derives a resolution. An `untracked` campaign therefore offers only "Adopt
 *   platform state" and "Record intended state".
 * - **"Record intended state" goes through `DecisionForm`.** It opens a `Dialog` holding
 *   `DecisionForm` in `intended_state` mode, which offers exactly the subject's shape fields and
 *   posts the complete intended state. Every other resolution posts at once with only
 *   `{ resolution }`; "Acknowledge removal" and "Platform is correct" close the discrepancy on
 *   the server immediately, so the refetched lists drop it.
 * - **Errors are the API's.** A 409 `subject_retired` reads "Computed from replaced data" and
 *   locks every control of that discrepancy (AAP 0.3.2); a 409 `discrepancy_resolved` reads
 *   "Already resolved"; any other failure, a 422 included, shows the API's own message.
 * - **No `alert` role.** AAP 0.5.5 reserves `role="alert"` for the discrepancy banner on
 *   `/actions`. Errors are plain critical banners that take focus; the outcome is a polite
 *   status message, and the list's own notices go to its polite status region.
 * - **Values come from the API only.** Each side of a discrepancy is rendered from its stored
 *   keys, statuses with `CAMPAIGN_STATUS_LABELS` and amounts with `formatMoney` in the subject's
 *   currency, and an amount finer than the currency's minor unit (a Google micro) also shows its
 *   exact digits, because C0 has no tolerance and rounding must not hide the mismatch it reports.
 *
 * Server state goes only through `../../api/queries` (AAP 0.11.1); the resolve mutation there
 * already invalidates recommendations, discrepancies and decisions. UI comes only from the
 * design-system index (AAP 0.4.4) and formatting only from `../../lib/format`.
 *
 * @example
 * ```tsx
 * // /decisions
 * <PageShell.Section id="discrepancies" title="Discrepancies">
 *   <DiscrepancyResolver />
 * </PageShell.Section>
 *
 * // A C0 card on /actions
 * <DiscrepancyResolution discrepancy={discrepancy} disabled={item.current.historical} />
 * ```
 *
 * @packageDocumentation
 */
import { useLocation } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import {
  isApiError,
  useDefaultRun,
  useDiscrepancyPages,
  useResolveDiscrepancy,
} from '../../api/queries';
import type { ApiError } from '../../api/queries';
import type {
  DecisionShape,
  Discrepancy,
  DiscrepancyKind,
  DiscrepancyState,
  DiscrepancyValue,
  Money,
  Resolution,
  SubjectType,
} from '../../api/types';
import {
  Banner,
  Button,
  Card,
  Dialog,
  EmptyState,
  Inline,
  Spinner,
  Stack,
} from '../../design-system';
import {
  UNAVAILABLE,
  currencyMinorDigits,
  formatCount,
  formatDecimal,
  formatInstant,
  formatMoney,
  sourceLabel,
} from '../../lib/format';
import { CAMPAIGN_STATUS_LABELS, DecisionForm } from './DecisionForm';
import { RETRY_LABEL, lastLoadedText, useRetry, useSettledFailure } from './readState';

// ---------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------

/**
 * The label of each C0 resolution, the single map every screen uses (AAP 0.5.4.4). It equals
 * `_RESOLUTION_LABELS` in the backend's `decisions.py`, and the end-to-end test
 * `web/tests/e2e/decisions.spec.ts` clicks the buttons by these labels.
 *
 * Typed `Record<Resolution, string>`, so a resolution added to the API contract fails to
 * compile until it is labelled here.
 */
export const RESOLUTION_LABELS: Readonly<Record<Resolution, string>> = Object.freeze({
  platform_correct: 'Platform is correct',
  decision_correct: 'Decision is correct',
  adopt_platform: 'Adopt platform state',
  record_intended: 'Record intended state',
  acknowledge_removal: 'Acknowledge removal',
});

/** Props of {@link DiscrepancyResolution}. */
export interface DiscrepancyResolutionProps {
  /** The discrepancy to resolve, as `GET /discrepancies` returned it. */
  discrepancy: Discrepancy;
  /**
   * Disables every resolution and keeps the intended-state dialog from opening, for example on
   * a historical C0 item computed from replaced data (AAP 0.3.2). Defaults to `false`.
   */
  disabled?: boolean;
  /**
   * Called once a resolution has been accepted, with the discrepancy as it now stands. For the
   * resolutions posted here that is the API's response (`resolved` or `awaiting_platform`).
   * "Record intended state" is posted by `DecisionForm`, which reports success without the
   * response, so that call receives this discrepancy advanced by the transition the API applies
   * to `record_intended`, `state: 'awaiting_platform'` and `resolution: 'record_intended'`; the
   * refetched discrepancy lists carry its newly recorded decided side.
   */
  onResolved?: (updated: Discrepancy) => void;
}

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/** Shown when a resolution is refused because the subject's dataset was replaced (AAP 0.3.2). */
const RETIRED_MESSAGE = 'Computed from replaced data';

/** Shown when the discrepancy was closed before this resolution reached the server. */
const RESOLVED_MESSAGE = 'Already resolved';

/** Shown for a failure that carries no message of its own. */
const FALLBACK_ERROR_MESSAGE = 'The resolution failed.';

/** The platform side of a `missing` discrepancy, worded as the backend's C0 item words it. */
const NOT_RETURNED_TEXT = 'not returned by the platform';

/** The decided side of an `untracked` discrepancy, which has no decision yet. */
const NOTHING_DECIDED_TEXT = 'nothing decided yet';

/**
 * A side recording an acknowledged removal (`tombstone: true`), worded as `DecisionLog` words
 * the same decided state on this screen.
 */
const REMOVAL_TEXT = 'Removal acknowledged';

/** A side whose status the platform did not report, worded as `DecisionLog` and `DecisionForm`. */
const STATUS_NOT_REPORTED = 'status not reported';

/** Joins the parts of one state, as `DecisionLog` and `DecisionForm` join them. */
const PART_SEPARATOR = ' · ';

/** The zone instants are shown in until the default run names `DISPLAY_TIMEZONE`. */
const FALLBACK_TIMEZONE = 'UTC';

/**
 * The most decimal places a stored amount carries: one Google micro, the scale of the
 * `numeric(18,6)` money columns (AAP 0.5.2).
 */
const MAX_AMOUNT_PLACES = 6;

/** How many digits of an integer micro amount are fractional: 1 unit = 1,000,000 micros. */
const MICRO_DIGITS = 6;

/** The outcome line shown after a resolution was accepted, by the state the API returned. */
const OUTCOME_TEXT: Readonly<Record<DiscrepancyState, string | null>> = Object.freeze({
  open: null,
  awaiting_platform: 'Waiting for the platform to match the decision',
  resolved: 'Resolved',
});

/**
 * The label of each discrepancy kind, as the card's entity line names it, and as a held card's
 * link to its blocking discrepancy names it on `/actions`.
 */
export const KIND_LABELS: Readonly<Record<DiscrepancyKind, string>> = Object.freeze({
  status: 'Status mismatch',
  budget: 'Budget mismatch',
  untracked: 'Untracked entity',
  missing: 'Missing from platform',
});

/** The label of each discrepancy state, as the card body shows it. */
const STATE_LABELS: Readonly<Record<DiscrepancyState, string>> = Object.freeze({
  open: 'Open',
  awaiting_platform: 'Awaiting platform',
  resolved: 'Resolved',
});

/**
 * The label of each subject type: a campaign, or a Google explicitly shared budget. It names an
 * unnamed subject ("Campaign 12") here and in a held card's blocking-discrepancy link.
 */
export const SUBJECT_TYPE_LABELS: Readonly<Record<SubjectType, string>> = Object.freeze({
  campaign: 'Campaign',
  budget: 'Shared budget',
});

/** A plain fixed-point decimal as the API serialises money, for example `"200.000001"`. */
const PLAIN_DECIMAL_PATTERN = /^-?\d+(?:\.(\d+))?$/;

// ---------------------------------------------------------------------------------------------
// Value rendering (pure; mirrors `render_value` in the backend's `analysis/discrepancy.py`)
// ---------------------------------------------------------------------------------------------

/** A trimmed, non-empty string, or `null`. */
function textOrNull(value: string | null | undefined): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * Whether an amount carries a non-zero digit beyond the currency's minor unit, such as a Google
 * budget of `"200.000001"` USD. `formatMoney` rounds to the minor unit, which would show that
 * amount and `"200.000000"` alike.
 */
function isFinerThanMinorUnit(amount: Money, currency: string | null): boolean {
  const match = PLAIN_DECIMAL_PATTERN.exec(amount.trim());
  const fraction = match?.[1] ?? '';
  return /[1-9]/.test(fraction.slice(currencyMinorDigits(currency)));
}

/**
 * One amount in the subject's currency through `formatMoney`. An amount finer than the
 * currency's minor unit is followed by its exact digits, "$200.00 (exactly 200.000001)": C0
 * compares at native precision with no tolerance (AAP 0.5.4.4), so a one-micro mismatch must
 * never render as two identical amounts.
 */
function amountText(amount: Money, currency: string | null): string {
  const formatted = formatMoney(amount, currency);
  return isFinerThanMinorUnit(amount, currency)
    ? `${formatted} (exactly ${formatDecimal(amount, MAX_AMOUNT_PLACES)})`
    : formatted;
}

/**
 * An integer micro amount as an exact decimal string, `200000001` → `"200.000001"`, built
 * from its digits so no binary float touches it. `null` for a value that is not a safe integer.
 */
function microsToDecimal(micros: number): Money | null {
  if (!Number.isSafeInteger(micros)) {
    return null;
  }
  const digits = String(Math.abs(micros)).padStart(MICRO_DIGITS + 1, '0');
  const sign = micros < 0 ? '-' : '';
  return `${sign}${digits.slice(0, -MICRO_DIGITS)}.${digits.slice(-MICRO_DIGITS)}`;
}

/** The normalised status of a side by its label; `"status not reported"` when absent. */
function statusText(value: DiscrepancyValue): string {
  const status = value.status;
  if (typeof status !== 'string') {
    return STATUS_NOT_REPORTED;
  }
  // A status outside the contract's vocabulary is shown as sent rather than mislabelled.
  return Object.hasOwn(CAMPAIGN_STATUS_LABELS, status) ? CAMPAIGN_STATUS_LABELS[status] : status;
}

/** Whether a side describes a shared-budget assignment rather than an amount. */
function hasAssignment(value: DiscrepancyValue): boolean {
  return value.budget_id !== undefined || value.budget_external_id !== undefined;
}

/**
 * A campaign's budget assignment: "shared budget {Google id}", the internal id when the Google
 * id is unknown, or "own budget (not shared)" when the side names no shared budget.
 */
function assignmentText(value: DiscrepancyValue): string {
  if (!hasAssignment(value)) {
    return `budget assignment ${UNAVAILABLE}`;
  }
  const externalId = textOrNull(value.budget_external_id);
  if (externalId !== null) {
    return `shared budget ${externalId}`;
  }
  const budgetId = value.budget_id;
  if (typeof budgetId === 'number') {
    return `shared budget (internal id ${budgetId})`;
  }
  return 'own budget (not shared)';
}

/**
 * The amounts of an `own_budget` or `budget` side, each named by its field. A side with no money
 * key, or whose reported amount is `null`, is "budget unavailable", never zero; a side that
 * reports neither budget field has "no budget set".
 *
 * @returns The parts to join, in display order.
 */
function moneyParts(value: DiscrepancyValue, currency: string | null): string[] {
  const { daily_budget: daily, total_budget: total, amount_micros: micros } = value;
  if (daily === undefined && total === undefined && micros === undefined) {
    return [`budget ${UNAVAILABLE}`];
  }
  const parts: string[] = [];
  if (typeof daily === 'string') {
    parts.push(`daily budget ${amountText(daily, currency)}`);
  }
  if (typeof total === 'string') {
    parts.push(`total budget ${amountText(total, currency)}`);
  }
  if (parts.length > 0) {
    return parts;
  }
  const exact = typeof micros === 'number' ? microsToDecimal(micros) : null;
  if (exact !== null) {
    return [`budget ${amountText(exact, currency)}`];
  }
  return micros === undefined ? ['no budget set'] : [`budget ${UNAVAILABLE}`];
}

/**
 * One side of a discrepancy as one line: the status label, then each amount in the subject's
 * currency, or the shared budget a member draws from, joined by " · ".
 *
 * - `status`: the status alone.
 * - `budget`: the assignment of a shared member (or of any side carrying assignment keys),
 *   otherwise the amounts.
 * - `untracked` and `missing`: the complete state the shape owns, status first except on a
 *   shared budget, which has none.
 * - A tombstone side is "Removal acknowledged", followed by the campaign's decided status
 *   (" · Ended") on a campaign subject, as `DecisionLog` renders the same decision.
 *
 * @param kind - The discrepancy kind.
 * @param shape - The subject's decision shape.
 * @param value - The stored side.
 * @param currency - The subject's account currency, or `null` when unknown.
 * @returns The display text.
 */
function describeValue(
  kind: DiscrepancyKind,
  shape: DecisionShape,
  value: DiscrepancyValue,
  currency: string | null,
): string {
  if (value.tombstone === true) {
    // acknowledge_removal writes status `ended` on campaigns; a shared budget has no status.
    return shape === 'budget'
      ? REMOVAL_TEXT
      : [REMOVAL_TEXT, statusText({ status: value.status ?? 'ended' })].join(PART_SEPARATOR);
  }
  if (kind === 'status') {
    return statusText(value);
  }
  const assignment = shape === 'shared_member' || hasAssignment(value);
  const budgetParts = assignment ? [assignmentText(value)] : moneyParts(value, currency);
  if (kind === 'budget' || shape === 'budget') {
    return budgetParts.join(PART_SEPARATOR);
  }
  return [statusText(value), ...budgetParts].join(PART_SEPARATOR);
}

/**
 * The "Platform shows {platform}, last decided {decided}" line of a discrepancy, worded as the
 * backend words the C0 item: an absent platform side (`missing`) is "not returned by the
 * platform" and an absent decided side (`untracked`) is "nothing decided yet".
 */
function comparisonText(discrepancy: Discrepancy): string {
  const { kind, shape, subject, platform_value: platform, decided_value: decided } = discrepancy;
  const currency =
    textOrNull(subject.currency) ??
    textOrNull(platform?.currency) ??
    textOrNull(decided?.currency);
  const platformText =
    platform === null ? NOT_RETURNED_TEXT : describeValue(kind, shape, platform, currency);
  const decidedText =
    decided === null ? NOTHING_DECIDED_TEXT : describeValue(kind, shape, decided, currency);
  return `Platform shows ${platformText}, last decided ${decidedText}`;
}

/** The card headline: the subject's name, else its type and internal id. */
function subjectTitle(discrepancy: Discrepancy): string {
  const { subject } = discrepancy;
  return textOrNull(subject.name) ?? `${SUBJECT_TYPE_LABELS[subject.type]} ${subject.id}`;
}

/** The card's entity line: "{source} · {Campaign or Shared budget} · {kind label}". */
function subjectMeta(discrepancy: Discrepancy): string {
  const { subject, kind } = discrepancy;
  const parts = [SUBJECT_TYPE_LABELS[subject.type], KIND_LABELS[kind]];
  if (subject.source !== null) {
    parts.unshift(sourceLabel(subject.source));
  }
  return parts.join(PART_SEPARATOR);
}

// ---------------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------------

/** Whether a failed resolution means the subject's dataset was replaced (409 `subject_retired`). */
function isRetiredError(error: unknown): boolean {
  return isApiError(error) && error.status === 409 && error.code === 'subject_retired';
}

/**
 * Whether a failed resolution means the discrepancy is already closed. The backend names it
 * 409 `discrepancy_resolved` (`decisions.py`, `ApiErrorCode` in `api/client.ts`); it sends no
 * `discrepancy_closed`, so that is the code read here, as `DecisionForm` reads it.
 */
function isResolvedError(error: unknown): boolean {
  return isApiError(error) && error.status === 409 && error.code === 'discrepancy_resolved';
}

/**
 * The text of a failed resolution: "Computed from replaced data" for `subject_retired`,
 * "Already resolved" for `discrepancy_resolved`, otherwise the API's own message, which for a
 * 422 such as `resolution_not_allowed` names the rule it broke.
 */
function resolveErrorMessage(error: unknown): string {
  if (isRetiredError(error)) {
    return RETIRED_MESSAGE;
  }
  if (isResolvedError(error)) {
    return RESOLVED_MESSAGE;
  }
  if (isApiError(error) || error instanceof Error) {
    return textOrNull(error.message) ?? FALLBACK_ERROR_MESSAGE;
  }
  return FALLBACK_ERROR_MESSAGE;
}

// ---------------------------------------------------------------------------------------------
// DiscrepancyResolution
// ---------------------------------------------------------------------------------------------

/**
 * The "Record intended state" dialog of one discrepancy: whether it is open, and which opening
 * it is. `session` grows with every opening, so a save that ends after its dialog was closed
 * can tell whether the dialog shown now is still its own.
 */
interface DialogSession {
  /** Whether the dialog is shown. */
  readonly open: boolean;
  /** The number of the latest opening; `0` before the first. */
  readonly session: number;
}

/** The dialog before its first opening. */
const CLOSED_DIALOG: DialogSession = { open: false, session: 0 };

/** The dialog opened anew: a session no earlier save belongs to. */
function openDialogSession(current: DialogSession): DialogSession {
  return { open: true, session: current.session + 1 };
}

/** The dialog closed, keeping its session; unchanged when it is closed already. */
function closeDialogSession(current: DialogSession): DialogSession {
  return current.open ? { ...current, open: false } : current;
}

/**
 * The resolution controls of one discrepancy: one `Button` per entry of
 * `discrepancy.resolutions`, in the API's order, the first `primary` and the rest `secondary`,
 * labelled from {@link RESOLUTION_LABELS}.
 *
 * - "Record intended state" opens a `Dialog` titled "Record intended state" holding
 *   `DecisionForm` (`mode="intended_state"`) for the discrepancy's subject, with its own Save and
 *   Cancel buttons. Saving closes the dialog; Cancel, Escape, the Close button and the backdrop
 *   close it without posting. A save that ends after its dialog was closed and reopened is
 *   still reported, and leaves the reopened dialog open with its edits.
 * - Every other resolution posts `POST /discrepancies/{id}/resolve` with `{ resolution }` at
 *   once. While it runs, the clicked button shows its loading state and the others are disabled.
 * - After a success, a polite status line names the outcome: "Resolved", or "Waiting for the
 *   platform to match the decision" for `awaiting_platform`. Then `onResolved` is called.
 * - A failure shows a critical banner, which takes focus, with "Computed from replaced data"
 *   (409 `subject_retired`), "Already resolved" (409 `discrepancy_resolved`) or the API's
 *   message. Either 409 disables every control while this discrepancy is shown, because the
 *   server would refuse any resolution of it again.
 *
 * The controls' state (the dialog, the outcome and those locks) belongs to the discrepancy's id:
 * a mount reused for another discrepancy starts fresh, so one subject's lock never disables
 * another's controls.
 */
export function DiscrepancyResolution(props: DiscrepancyResolutionProps) {
  return <DiscrepancyResolutionBody key={props.discrepancy.id} {...props} />;
}

/**
 * The body of {@link DiscrepancyResolution} for one discrepancy id. Its state and its mutation
 * live exactly as long as that id, because `DiscrepancyResolution` keys it by the id.
 *
 * @param props - The props `DiscrepancyResolution` received, unchanged.
 */
function DiscrepancyResolutionBody({
  discrepancy,
  disabled = false,
  onResolved,
}: DiscrepancyResolutionProps) {
  const resolve = useResolveDiscrepancy();
  const [dialog, setDialog] = useState<DialogSession>(CLOSED_DIALOG);
  const [outcome, setOutcome] = useState<DiscrepancyState | null>(null);

  const error = resolve.error;
  // A 409 subject_retired is final for this discrepancy (AAP 0.3.2), and so is a closed one: the
  // server refuses every further resolution of it, so no control stays enabled. Every control
  // is disabled from then on, so no later attempt can reset the error that set the lock.
  const lockedByServer = isRetiredError(error) || isResolvedError(error);
  const locked = disabled || lockedByServer;
  const pendingResolution = resolve.isPending ? resolve.variables.body.resolution : null;

  // A clicked button is disabled while its request runs, which drops keyboard focus to the page.
  // When the request fails, focus moves to the error instead, so keyboard and screen-reader users
  // land on the message (WCAG 2.2 SC 2.4.3, 3.3.1) without a role="alert" live region, which
  // AAP 0.5.5 reserves for the discrepancy banner on /actions.
  const errorRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (error !== null) {
      errorRef.current?.focus();
    }
  }, [error]);

  // A success that leaves this discrepancy on screen (awaiting_platform) re-enables the clicked
  // button; focus goes back to it when the loading state dropped focus to the page, so keyboard
  // and screen-reader users stay on the control they used (WCAG 2.2 SC 2.4.3). A closed
  // discrepancy leaves the lists, and the browser's focus navigation starting point then keeps
  // the user's place, so nothing is focused for it.
  const buttonRefs = useRef(new Map<Resolution, HTMLButtonElement>());
  const [refocus, setRefocus] = useState<Resolution | null>(null);
  useEffect(() => {
    if (refocus === null || resolve.isPending) {
      return;
    }
    setRefocus(null);
    const button = buttonRefs.current.get(refocus);
    const active = document.activeElement;
    const focusLost = active === null || active === document.body;
    if (button !== undefined && button.isConnected && !button.disabled && focusLost) {
      button.focus();
    }
  }, [refocus, resolve.isPending]);

  const post = (resolution: Resolution) => {
    setOutcome(null);
    // mutateAsync, unlike mutate's per-call callbacks, settles even when this card unmounts
    // first: a resolution that closes the discrepancy refetches the list without it before the
    // request settles here, and onResolved must still be told. A failure is already held in
    // resolve.error, which renders the banner, so the rejection needs no handling beyond that.
    void resolve.mutateAsync({ id: discrepancy.id, body: { resolution } }).then(
      (updated) => {
        setOutcome(updated.state);
        if (updated.state !== 'resolved') {
          setRefocus(resolution);
        }
        onResolved?.(updated);
      },
      () => undefined,
    );
  };

  const openIntendedState = () => {
    if (locked) {
      return;
    }
    // A failure of an earlier resolution no longer describes what the dialog is about to post.
    resolve.reset();
    setOutcome(null);
    setDialog(openDialogSession);
  };

  const closeIntendedState = () => {
    setDialog(closeDialogSession);
  };

  // DecisionForm calls the onDone it had when the user saved, even after its dialog was closed,
  // so a save that ends late closes only the opening it was made in (`session`): a dialog
  // reopened in the meantime keeps its edits. The intended state is recorded either way, so the
  // outcome is still shown and reported.
  const handleIntendedSaved = (session: number) => {
    setDialog((current) => (current.session === session ? closeDialogSession(current) : current));
    // The API moves a record_intended discrepancy to awaiting_platform (decisions.py
    // resolve_discrepancy); DecisionForm reports success without the response body, so the
    // update passed on is this discrepancy advanced by exactly that transition.
    setOutcome('awaiting_platform');
    onResolved?.({ ...discrepancy, state: 'awaiting_platform', resolution: 'record_intended' });
  };

  const handleClick = (resolution: Resolution) => {
    if (locked || pendingResolution !== null) {
      return;
    }
    if (resolution === 'record_intended') {
      openIntendedState();
      return;
    }
    post(resolution);
  };

  const errorText = error === null ? null : resolveErrorMessage(error);
  const outcomeText = outcome === null ? null : OUTCOME_TEXT[outcome];
  // The opening this render's intended-state form belongs to.
  const formSession = dialog.session;

  return (
    // BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive and feature code
    // carries no CSS (AAP 0.4.4), so text lines are <div> elements spaced by the Stack gap
    // alone, as in DecisionForm. The error's wrapper is unstyled and exists only as the focus
    // target after a failed resolution.
    <Stack gap={2}>
      {errorText === null ? null : (
        <div ref={errorRef} tabIndex={-1}>
          <Banner tone="critical" title={errorText} />
        </div>
      )}
      <Inline gap={2}>
        {discrepancy.resolutions.map((resolution, index) => (
          <Button
            key={resolution}
            ref={(element: HTMLButtonElement) => {
              buttonRefs.current.set(resolution, element);
              return () => {
                buttonRefs.current.delete(resolution);
              };
            }}
            variant={index === 0 ? 'primary' : 'secondary'}
            loading={pendingResolution === resolution}
            disabled={locked || (pendingResolution !== null && pendingResolution !== resolution)}
            onClick={() => {
              handleClick(resolution);
            }}
          >
            {RESOLUTION_LABELS[resolution]}
          </Button>
        ))}
      </Inline>
      {/*
        A polite status region, mounted empty ahead of its first message so the outcome is
        announced when it appears (WCAG 2.2 SC 4.1.3); Stack gives an empty status child no
        space. It never carries role="alert", which AAP 0.5.5 reserves for the discrepancy
        banner on /actions.
      */}
      <div role="status">{outcomeText}</div>
      <Dialog
        open={dialog.open}
        title={RESOLUTION_LABELS.record_intended}
        onClose={closeIntendedState}
      >
        <DecisionForm
          mode="intended_state"
          subject={{ type: discrepancy.subject.type, id: discrepancy.subject.id }}
          discrepancy={discrepancy}
          currency={discrepancy.subject.currency}
          onDone={() => {
            handleIntendedSaved(formSession);
          }}
          onCancel={closeIntendedState}
          disabled={locked}
        />
      </Dialog>
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// DiscrepancyResolver
// ---------------------------------------------------------------------------------------------

/** Accessible label of the spinner shown until the first page of the list arrives. */
const LOADING_LABEL = 'Loading discrepancies';

/** Title of the failure shown when the first read of the list fails. */
const LOAD_ERROR_TITLE = 'Discrepancies could not be loaded';

/** Title of the warning shown when a refetch of a list already on screen fails. */
const REFRESH_ERROR_TITLE = 'Discrepancies could not be refreshed';

/** Title of the warning shown when "Show more" fails; the list loaded so far stays. */
const MORE_ERROR_TITLE = 'More discrepancies could not be loaded';

/** Reason shown for a failed read that carries no message of its own. */
const READ_ERROR_FALLBACK = 'The discrepancy list could not be read.';

/** Title of the empty state, shown only when the latest read succeeded. */
const EMPTY_TITLE = 'No open discrepancies';

/** Reason of the empty state. */
const EMPTY_REASON = 'Platform state matches every last decision.';

/**
 * Accessible name of the card list. Links reach a card by its own element id
 * ({@link cardId}), never through the list.
 */
const LIST_LABEL = 'Discrepancies to resolve';

/** Label of the button that loads the next page of the list. */
const SHOW_MORE_LABEL = 'Show more discrepancies';

/**
 * The start of each card's element id, `discrepancy-{id}`, which a link to the card names as its
 * location hash (`/decisions#discrepancy-{id}`, built by `features/actions/BlockingDiscrepancies`).
 */
const CARD_ID_PREFIX = 'discrepancy-';

/** A discrepancy id as the API sends it and a card's element id spells it: a positive integer. */
const DISCREPANCY_ID_PATTERN = /^[1-9][0-9]*$/u;

/** The element id of a discrepancy's card, `discrepancy-{id}`, from its id or the id's digits. */
function cardId(id: Discrepancy['id'] | string): string {
  return `${CARD_ID_PREFIX}${String(id)}`;
}

/**
 * The id of the discrepancy a location hash links to, `"12"` for `discrepancy-12`, or `null`
 * for any other hash, such as the page's own `discrepancies` section anchor. `hash` is the
 * router's decoded hash, without its `#`.
 */
function linkedDiscrepancyId(hash: string): string | null {
  if (!hash.startsWith(CARD_ID_PREFIX)) {
    return null;
  }
  const id = hash.slice(CARD_ID_PREFIX.length);
  return DISCREPANCY_ID_PATTERN.test(id) ? id : null;
}

/**
 * The status line for a linked discrepancy that a fully read list does not hold: "Discrepancy
 * {id} is no longer unresolved." It was resolved, by the worker or by hand, after the link was
 * made. When the list also leaves out discrepancies whose subject cannot be loaded
 * ({@link unavailableCount}), it may be one of those, and the line names both possibilities.
 */
function linkedAbsentText(id: string, unavailable: number): string {
  return unavailable > 0
    ? `Discrepancy ${id} is not listed: it is no longer unresolved, or its campaign or ` +
        'shared budget could not be loaded.'
    : `Discrepancy ${id} is no longer unresolved.`;
}

/** The text of a failed list read: the API's message, or a generic sentence without one. */
function readErrorMessage(error: unknown): string {
  return (isApiError(error) ? textOrNull(error.message) : null) ?? READ_ERROR_FALLBACK;
}

/**
 * The summary line of a loaded list: "Showing {n} of {total} unresolved discrepancies", or
 * "Showing {n} unresolved discrepancies" when the API sent no total. The total counts every
 * unresolved discrepancy of the active datasets, as the `/actions` banner does, so it can exceed
 * what every page together lists when a subject cannot be loaded. While pages remain to load the
 * rest may still appear; once every page is read, {@link unavailableText} explains the
 * difference beneath this line.
 */
function summaryText(shown: number, total: number | undefined): string {
  const noun = (total ?? shown) === 1 ? 'unresolved discrepancy' : 'unresolved discrepancies';
  return total === undefined
    ? `Showing ${formatCount(shown)} ${noun}`
    : `Showing ${formatCount(shown)} of ${formatCount(total)} ${noun}`;
}

/**
 * How many unresolved discrepancies a list can never show: the API's total less the `shown`
 * discrepancies, once every page is read (`complete`). `GET /discrepancies` counts in
 * `X-Total-Count` the rows whose subject cannot be loaded but leaves them out of every page, so
 * only a fully read list can tell how many those are. `0` while pages remain, when the API sent
 * no total, or when the total does not exceed what is shown.
 */
function unavailableCount(shown: number, total: number | undefined, complete: boolean): number {
  return complete && total !== undefined && total > shown ? total - shown : 0;
}

/**
 * The line explaining the `unavailable` discrepancies ({@link unavailableCount}) a fully read
 * list leaves out. Beneath the summary of a `listed` list it gives the difference: "1 cannot be
 * shown because its campaign or shared budget could not be loaded." With nothing listed it
 * stands in place of the summary and so names the total: "2 unresolved discrepancies cannot be
 * shown because their campaigns or shared budgets could not be loaded."
 */
function unavailableText(unavailable: number, listed: boolean): string {
  const single = unavailable === 1;
  const noun = single ? 'unresolved discrepancy' : 'unresolved discrepancies';
  const subjects = single ? 'its campaign or shared budget' : 'their campaigns or shared budgets';
  const count = listed ? formatCount(unavailable) : `${formatCount(unavailable)} ${noun}`;
  return `${count} cannot be shown because ${subjects} could not be loaded.`;
}

/** A "Show more" that has settled, kept until focus has been placed after it. */
interface SettledMore {
  /** The index, in the list, of the first discrepancy the load added. */
  firstNew: number;
  /** The query's `dataUpdatedAt` the load settled with; the render that shows it moves focus. */
  dataUpdatedAt: number;
  /** The query's `errorUpdatedAt` the load settled with. */
  errorUpdatedAt: number;
  /** The error the load failed with, or `null` when it succeeded. */
  error: ApiError | null;
}

/** A closed discrepancy to announce, kept until the next successful read replaces the list. */
interface Closure {
  /** The text announced: "{subject} resolved". */
  text: string;
  /** When the resolution was reported, in milliseconds since the epoch. */
  at: number;
}

/** Props of {@link DiscrepancyCard}. */
interface DiscrepancyCardProps {
  /** The discrepancy shown. */
  discrepancy: Discrepancy;
  /** The IANA zone the detection instant is formatted in. */
  displayTimezone: string;
  /** Passed to the card's {@link DiscrepancyResolution} as its `onResolved`. */
  onResolved: (updated: Discrepancy) => void;
}

/**
 * One resolver card: the subject as the headline, its source, type and kind as the entity line,
 * the state, the platform and decided values and the detection time in the body, and the
 * resolution controls in the footer. Every line is built from API values only.
 */
function DiscrepancyCard({ discrepancy, displayTimezone, onResolved }: DiscrepancyCardProps) {
  return (
    <Card id={cardId(discrepancy.id)}>
      <Card.Header
        headingLevel={3}
        title={subjectTitle(discrepancy)}
        meta={subjectMeta(discrepancy)}
      />
      <Card.Body>
        <div>{STATE_LABELS[discrepancy.state]}</div>
        <div>{comparisonText(discrepancy)}</div>
        <div>{`Detected ${formatInstant(discrepancy.detected_at, displayTimezone)}`}</div>
      </Card.Body>
      <Card.Footer>
        <DiscrepancyResolution discrepancy={discrepancy} onResolved={onResolved} />
      </Card.Footer>
    </Card>
  );
}

/** The resolver's paged list query, as `useDiscrepancyPages()` returns it. */
type DiscrepancyPagesQuery = ReturnType<typeof useDiscrepancyPages>;

/** A page load {@link useLinkedDiscrepancy} made to reach the card a link names. */
interface LinkedLoad {
  /** Whether the load is still in flight. */
  readonly pending: boolean;
  /** The query's `dataUpdatedAt` the load settled with; `0` while it is in flight. */
  readonly dataUpdatedAt: number;
  /** The query's `errorUpdatedAt` the load settled with; `0` while it is in flight. */
  readonly errorUpdatedAt: number;
  /** The error the load failed with; `null` while it is in flight and after a success. */
  readonly error: ApiError | null;
}

/** A {@link LinkedLoad} that has not settled yet. */
const PENDING_LINKED_LOAD: LinkedLoad = Object.freeze({
  pending: true,
  dataUpdatedAt: 0,
  errorUpdatedAt: 0,
  error: null,
});

/** What {@link useLinkedDiscrepancy} tells the resolver. */
interface LinkedDiscrepancy {
  /** The id of the discrepancy the location hash links to, or `null` when it names none. */
  readonly id: string | null;
  /** Whether that discrepancy is among the loaded ones, so its card is rendered. */
  readonly listed: boolean;
  /** Whether a page load toward it is in flight. */
  readonly loading: boolean;
  /** The failure of the latest such load while it is still the query's error; else `null`. */
  readonly error: ApiError | null;
}

/**
 * Brings the card a link names into view: `/decisions#discrepancy-{id}`, the link a held card on
 * `/actions` gives a blocker that has no C0 card in its run (`BlockingDiscrepancies`). The list
 * is paged, so on arrival the card may not be loaded yet, and the router's own scroll to the
 * hash, made once the route has rendered, finds no element.
 *
 * The hash is read through the router (`useLocation`), so a client-side `Link` navigation is
 * seen as well as a full page load, and a hash that changes while the resolver is shown is
 * followed.
 *
 * - **Loading.** While the loaded pages do not hold the discrepancy and the API names a next
 *   page, the next page is loaded with `fetchNextPage`: the same `DISCREPANCY_PAGE_SIZE`-row load
 *   "Show more" makes, under the same paging guards, one load at a time, until the card is
 *   loaded or the list ends. The whole list is never read in one request. No load starts while
 *   the list is being read already (a poll, a retry, or "Show more", which `busy` reports),
 *   before the render that shows the previous load, or while the list's latest read failed; a
 *   failed load is therefore tried again only after a later read of the list succeeds.
 * - **Scrolling.** Once the card has rendered, it is scrolled into view, once per hash: polls
 *   and later pages never scroll again, while a new hash does, and so does the same hash
 *   followed again after it was cleared. Focus is not moved.
 *
 * @param query - The resolver's `useDiscrepancyPages()` result.
 * @param busy - Whether "Show more" is loading a page.
 * @returns The linked discrepancy's id, whether it is listed, and its loads' state.
 */
function useLinkedDiscrepancy(query: DiscrepancyPagesQuery, busy: boolean): LinkedDiscrepancy {
  const hash = useLocation({ select: (location) => location.hash });
  const id = linkedDiscrepancyId(hash);
  const { data: list, error, hasNextPage, isFetching, fetchNextPage } = query;
  const { dataUpdatedAt, errorUpdatedAt } = query;
  const listed =
    id !== null &&
    list !== undefined &&
    list.discrepancies.some((discrepancy) => String(discrepancy.id) === id);

  const [load, setLoad] = useState<LinkedLoad | null>(null);
  // TanStack notifies observers on a later tick, so a settled load waits for the render that
  // shows it; deciding from the list before it would load a page past the one just read.
  const awaitingLoad =
    load !== null &&
    (load.pending || dataUpdatedAt < load.dataUpdatedAt || errorUpdatedAt < load.errorUpdatedAt);
  const loadNext =
    id !== null &&
    list !== undefined &&
    !listed &&
    hasNextPage &&
    !isFetching &&
    !busy &&
    error === null &&
    !awaitingLoad;

  useEffect(() => {
    if (!loadNext) {
      return;
    }
    setLoad(PENDING_LINKED_LOAD);
    // `cancelRefetch: false` joins a poll that started since this render instead of cancelling
    // it, so the list is never read twice at once.
    void fetchNextPage({ cancelRefetch: false }).then((result) => {
      setLoad({
        pending: false,
        dataUpdatedAt: result.dataUpdatedAt,
        errorUpdatedAt: result.errorUpdatedAt,
        error: result.isFetchNextPageError ? result.error : null,
      });
    });
  }, [loadNext, fetchNextPage]);

  // The hash whose card was scrolled to; cleared with the hash, so a link followed again scrolls.
  const scrolledFor = useRef<string | null>(null);
  useEffect(() => {
    if (id === null) {
      scrolledFor.current = null;
      return;
    }
    if (!listed || scrolledFor.current === id) {
      return;
    }
    const card = document.getElementById(cardId(id));
    if (card === null) {
      return;
    }
    scrolledFor.current = id;
    // Environments without layout, such as jsdom, do not implement scrollIntoView.
    if (typeof card.scrollIntoView === 'function') {
      card.scrollIntoView({ block: 'start' });
    }
  }, [id, listed]);

  return {
    id,
    listed,
    loading: load?.pending === true,
    error: load?.error != null && error === load.error ? load.error : null,
  };
}

/**
 * The `/decisions` resolver: the open and awaiting-platform discrepancies of the active datasets,
 * oldest `detected_at` first as the API returns them, a page at a time (`useDiscrepancyPages`,
 * `DISCREPANCY_PAGE_SIZE` rows per page), one {@link Card} each with
 * `id="discrepancy-{id}"`, the target of links to a blocking discrepancy, in a list labelled
 * "Discrepancies to resolve".
 *
 * Each card's heading is an `<h3>` below the page's "Discrepancies" section heading. Its entity
 * line reads "{source} · {Campaign or Shared budget} · {kind}", and its body the state ("Open" or
 * "Awaiting platform"), "Platform shows {platform}, last decided {decided}" and "Detected
 * {instant}", formatted in the default run's `display_timezone`, or in UTC, so labelled, until
 * that run is known. The footer holds {@link DiscrepancyResolution}.
 *
 * **Paging** (AAP 0.1.1 performance). Only the pages loaded so far are fetched and mounted.
 * Below the list a "Show more discrepancies" `Button` loads the next page while the API names
 * one; it shows its loading state meanwhile, and once the page is shown focus returns to it, or,
 * after the last page removed it, moves to the first discrepancy the page added, unless the user
 * moved focus elsewhere (WCAG 2.2 SC 2.4.3). Every discrepancy stays reachable.
 *
 * **Linked discrepancy** (AAP 0.5.5 "with the blocking discrepancy linked"). When the location
 * hash names a card, `#discrepancy-{id}` as a held card's link on `/actions` does, on arrival or
 * by a later change, the resolver loads the next page while the pages loaded so far do not hold
 * that discrepancy and the API names one, a page at a time as "Show more" would, with "Show more"
 * in its loading state meanwhile. Once the card has rendered it is scrolled into view, once per
 * hash, without moving focus. When every page is read and none holds it, the status region says
 * so (see below). A failed load reads as a failed "Show more".
 *
 * **One polite status region** (WCAG 2.2 SC 4.1.3). The resolver renders a single
 * `role="status"` element in every state, from the first render, outside the card list, so
 * each outcome lands in a region assistive technology already watches and is announced
 * politely. It never carries `role="alert"`, which AAP 0.5.5 reserves for the discrepancy banner
 * on `/actions`. The `Spinner` sits outside it, because it is a status element of its own. The
 * region holds, as they apply:
 *
 * - the first read failed: an `EmptyState` "Discrepancies could not be loaded" with the API's
 *   message and a "Retry" `Button`. It stays in place while the read runs again, by poll or by
 *   Retry, so it never flickers back to the spinner, and focus returns to Retry once a retry
 *   the user started settles;
 * - a refetch of a loaded list failed: a warning `Banner` "Discrepancies could not be
 *   refreshed" with the API's message, saying the list shown is the state as last loaded at
 *   {instant}, or that none were open then when that read listed and counted none, with
 *   "Retry". A discrepancy left on screen still blocks the items that depend on it (AAP 0.5.4.2
 *   P1), so the list and its summary lines below are kept, and an empty list from an earlier
 *   read is never presented as the current state;
 * - the latest read succeeded, every page is read, nothing is listed and the API's
 *   `X-Total-Count` is 0 or absent: an `EmptyState` "No open discrepancies", "Platform state
 *   matches every last decision.";
 * - a loaded list: "Showing {n} of {total} unresolved discrepancies", the total from the API's
 *   `X-Total-Count`, or "Showing {n} unresolved discrepancies" without one. While "Show more"
 *   remains, the rest may still load, so nothing more is said;
 * - every page is read and the total exceeds what is listed: the API counts, but cannot list, a
 *   discrepancy whose campaign or shared budget cannot be loaded, so a line beneath the summary
 *   reads "{total − n} cannot be shown because their campaigns or shared budgets could not be
 *   loaded.", or, with nothing listed, stands in place of the summary as "{total} unresolved
 *   discrepancies cannot be shown because …". A positive total is never shown as "No open
 *   discrepancies", which would contradict the `/actions` banner's count (AAP 0.5.4.2 P1);
 * - every page is read and none holds the discrepancy the hash links to: "Discrepancy {id} is
 *   no longer unresolved.", or, while some discrepancies cannot be shown, "Discrepancy {id} is
 *   not listed: it is no longer unresolved, or its campaign or shared budget could not be
 *   loaded.";
 * - "Show more", or a page load toward the linked discrepancy, failed: a warning `Banner` "More
 *   discrepancies could not be loaded" with the API's message; "Show more" stays to try again;
 * - a resolution closed a discrepancy: "{subject} resolved", until the next successful read.
 *   The closed card leaves the refetched list before its own status line could be announced.
 *
 * The list refreshes itself every 30 s while shown (`useDiscrepancyPages`), so discrepancies the
 * worker opens or auto-resolves appear without a reload.
 */
export function DiscrepancyResolver() {
  const query = useDiscrepancyPages();
  const defaultRun = useDefaultRun();
  const displayTimezone = textOrNull(defaultRun.data?.display_timezone) ?? FALLBACK_TIMEZONE;
  const list = query.data;
  const { dataUpdatedAt, errorUpdatedAt } = query;
  const failure = useSettledFailure(query.error, query.fetchStatus !== 'idle', list !== undefined);
  const { retrying, buttonRef: retryButtonRef, retry } = useRetry(query.refetch);

  const [closure, setClosure] = useState<Closure | null>(null);
  const handleResolved = (updated: Discrepancy) => {
    if (updated.state === 'resolved') {
      setClosure({ text: `${subjectTitle(updated)} resolved`, at: Date.now() });
    }
  };

  // "Show more": the button is loading, hence disabled, until the page settles; then focus is
  // placed once the render that shows the page has committed.
  const [loadingMore, setLoadingMore] = useState(false);
  const [settledMore, setSettledMore] = useState<SettledMore | null>(null);
  const focusedMoreRef = useRef<SettledMore | null>(null);
  const showMoreRef = useRef<HTMLButtonElement>(null);
  const firstNewRef = useRef<HTMLLIElement>(null);
  const linked = useLinkedDiscrepancy(query, loadingMore);

  useEffect(() => {
    if (settledMore === null || focusedMoreRef.current === settledMore) {
      return;
    }
    if (dataUpdatedAt < settledMore.dataUpdatedAt || errorUpdatedAt < settledMore.errorUpdatedAt) {
      // TanStack notifies observers on a later tick; wait for the render that shows the page.
      return;
    }
    focusedMoreRef.current = settledMore;
    const active = document.activeElement;
    if (active !== null && active !== document.body) {
      return;
    }
    const button = showMoreRef.current;
    if (button !== null && button.isConnected && !button.disabled) {
      button.focus();
      return;
    }
    const firstNew = firstNewRef.current;
    if (firstNew !== null && firstNew.isConnected) {
      firstNew.focus();
    }
  }, [settledMore, dataUpdatedAt, errorUpdatedAt]);

  const showMore = async (): Promise<void> => {
    if (list === undefined || loadingMore || linked.loading) {
      return;
    }
    const firstNew = list.discrepancies.length;
    setLoadingMore(true);
    try {
      const result = await query.fetchNextPage();
      setSettledMore({
        firstNew,
        dataUpdatedAt: result.dataUpdatedAt,
        errorUpdatedAt: result.errorUpdatedAt,
        error: result.isFetchNextPageError ? result.error : null,
      });
    } finally {
      setLoadingMore(false);
    }
  };

  const retryButton = (
    <Button
      ref={retryButtonRef}
      loading={retrying}
      onClick={() => {
        void retry();
      }}
    >
      {RETRY_LABEL}
    </Button>
  );

  // A failed "Show more", or a failed page load toward a linked card, stays its own notice while
  // that error is the query's current one; any other error on a loaded list is a failed refresh.
  const moreError =
    settledMore?.error != null && query.error === settledMore.error
      ? settledMore.error
      : linked.error;
  const refreshError = list !== undefined && query.error !== moreError ? query.error : null;
  const notices: ReactNode[] = [];
  if (list === undefined) {
    if (failure !== null) {
      notices.push(
        <EmptyState key="load-error" title={LOAD_ERROR_TITLE} action={retryButton}>
          {readErrorMessage(failure)}
        </EmptyState>,
      );
    }
  } else {
    const shown = list.discrepancies.length;
    const complete = !query.hasNextPage;
    const unavailable = unavailableCount(shown, list.total, complete);
    // Nothing is open only when every page is read, none is listed and the API counts none: a
    // positive total over an empty list means its subjects could not be loaded, never that the
    // platform matches every decision.
    const noneOpen = complete && shown === 0 && (list.total ?? 0) === 0;
    if (refreshError !== null) {
      const loadedAt = lastLoadedText(dataUpdatedAt, displayTimezone);
      const asLoaded = noneOpen
        ? `No discrepancies were open when the list was last loaded ${loadedAt}.`
        : `Showing the list as last loaded ${loadedAt}.`;
      notices.push(
        <Banner
          key="refresh-error"
          tone="warning"
          title={REFRESH_ERROR_TITLE}
          actions={retryButton}
        >
          <Stack gap={1}>
            <div>{readErrorMessage(refreshError)}</div>
            <div>{asLoaded}</div>
          </Stack>
        </Banner>,
      );
    } else if (noneOpen) {
      notices.push(
        <EmptyState key="empty" title={EMPTY_TITLE}>
          {EMPTY_REASON}
        </EmptyState>,
      );
    }
    // With nothing listed, the unavailable line names the total in place of the summary.
    if (shown > 0 || (!noneOpen && unavailable === 0)) {
      notices.push(<div key="summary">{summaryText(shown, list.total)}</div>);
    }
    if (unavailable > 0) {
      notices.push(<div key="unavailable">{unavailableText(unavailable, shown > 0)}</div>);
    }
    if (linked.id !== null && complete && !linked.listed) {
      notices.push(<div key="linked-absent">{linkedAbsentText(linked.id, unavailable)}</div>);
    }
    if (moreError !== null) {
      notices.push(
        <Banner key="more-error" tone="warning" title={MORE_ERROR_TITLE}>
          {readErrorMessage(moreError)}
        </Banner>,
      );
    }
  }
  if (closure !== null && dataUpdatedAt <= closure.at) {
    notices.push(<div key="closure">{closure.text}</div>);
  }

  return (
    // BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text or live-region primitive and
    // feature code carries no CSS (AAP 0.4.4), so the status region and its text lines are
    // unstyled <div> elements spaced by the Stack gap alone; Stack takes the region out of the
    // column while it is empty.
    <Stack gap={4}>
      {list === undefined && failure === null ? <Spinner label={LOADING_LABEL} /> : null}
      <div role="status">{notices.length === 0 ? null : <Stack gap={3}>{notices}</Stack>}</div>
      {list !== undefined && list.discrepancies.length > 0 ? (
        <Stack as="ul" gap={4} aria-label={LIST_LABEL}>
          {list.discrepancies.map((discrepancy, index) => {
            const isFirstNew = settledMore !== null && index === settledMore.firstNew;
            return (
              <li
                key={discrepancy.id}
                ref={isFirstNew ? firstNewRef : undefined}
                tabIndex={isFirstNew ? -1 : undefined}
              >
                <DiscrepancyCard
                  discrepancy={discrepancy}
                  displayTimezone={displayTimezone}
                  onResolved={handleResolved}
                />
              </li>
            );
          })}
        </Stack>
      ) : null}
      {list !== undefined && query.hasNextPage ? (
        <Inline gap={2}>
          <Button
            ref={showMoreRef}
            variant="secondary"
            loading={loadingMore || linked.loading}
            onClick={() => {
              void showMore();
            }}
          >
            {SHOW_MORE_LABEL}
          </Button>
        </Inline>
      ) : null}
    </Stack>
  );
}
