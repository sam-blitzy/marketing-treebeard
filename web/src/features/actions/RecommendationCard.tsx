/**
 * `RecommendationCard`: one "do this because of this" action on `/actions` (AAP 0.5.5
 * Recommendation card; AAP 0.1.2: the card leads with the action, and every number sits beside
 * the claim it supports).
 *
 * Layout, top to bottom:
 *
 * 1. **Header** (`Card.Header`): the rank, the action as the headline (the card's accessible
 *    name), and the entity name with its source, for example "Generic CRM — US · Google".
 * 2. **Body** (`Card.Body`):
 *    - for a historical item (`current.historical`), the "Computed from replaced data" badge and
 *      the original provenance, for example "Simulated LinkedIn data, since replaced by live"
 *      (AAP 0.3.2: the frozen provenance and the current status are shown apart);
 *    - for an item held by a discrepancy, why it is held ("Resolve discrepancy first") and a
 *      link to the Campaigns discrepancy group (AAP 0.5.4.2 P1, 0.5.5 "with the blocking
 *      discrepancy linked"). C0 items are exempt from holds and never show it, and neither do
 *      historical items, whose overlay hold is the replaced data the badge already states;
 *    - the reason sentence, then the section-specific fields the caller passes as `children`;
 *    - the {@link EvidenceTable}, the window chip "{start} – {end} · {N} days · {source
 *      boundaries}" and the flags row ({@link FlagList}: recorded plus current flags).
 * 3. **Footer** (`Card.Footer`):
 *    - C0 discrepancies render the caller's `resolver` instead of feedback actions, because C0
 *      is exempt from dismiss, snooze and done (AAP 0.5.4.2, 0.5.4.4);
 *    - every other item renders {@link RecommendationActions}: "Record decision" on items with
 *      a `decision_subject` (C1, C1b, C2 "Scale", B1, B2, B3), "Mark done" on every other
 *      action, and "Dismiss" and "Snooze for 7 days" on all of them.
 *
 * A historical item keeps its card readable, but every footer action is disabled, the C0
 * resolver too (`resolver({ disabled: true })`). A 409 `subject_retired` from a screen that
 * went stale before its refetch locks the actions the same way and shows the same message.
 *
 * Every text on the card (action, reason, flag messages, held reason, entity name) comes from
 * the API, so no threshold, window length or rule wording is written into the client
 * (AAP 0.5.4.1). Server state goes only through `../../api/queries` (AAP 0.11.1), and UI comes
 * only from the design-system index (AAP 0.4.4): no raw buttons, headings or tables, no inline
 * styles and no stylesheet; layout comes from `Stack` and `Inline`.
 *
 * @packageDocumentation
 */
import { useEffect, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import { isApiError, useFeedback } from '../../api/queries';
import type { DataStatus, EntityView, FeedbackKind, Item, ProvenanceEntry } from '../../api/types';
import { Badge, Banner, Button, Card, Dialog, Inline, Stack } from '../../design-system';
import { formatWindowChip, sourceLabel } from '../../lib/format';
import { DecisionForm } from '../decisions/DecisionForm';
import { EvidenceTable } from './EvidenceTable';
import { decisionContextFor, type DecisionContext } from './evidenceExtra';
import { FlagList } from './FlagList';
import { DISCREPANCY_GROUP_ANCHOR, isHeld } from './sections/SectionFrame';

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/**
 * The badge on a historical item, and the message a 409 `subject_retired` shows (AAP 0.5.5:
 * "a 'Computed from replaced data' badge … A 409 `subject_retired` from a stale screen is shown
 * as the same message").
 */
export const HISTORICAL_LABEL = 'Computed from replaced data';

/**
 * The label of each feedback action on a card footer (AAP 0.5.5), which the end-to-end specs
 * find the buttons by. "Snooze for 7 days" is the label AAP 0.5.5 fixes; the stored suppression
 * length is the server's (`feedback.snooze_days` in `config/thresholds.yaml`), which the
 * response's `until` carries.
 */
export const FEEDBACK_LABELS: Readonly<Record<FeedbackKind, string>> = Object.freeze({
  done: 'Mark done',
  dismiss: 'Dismiss',
  snooze: 'Snooze for 7 days',
});

/**
 * The status line a card shows once its feedback is recorded, until the refetched
 * recommendations hide the item (`current.hidden_by`).
 */
export const FEEDBACK_STATUS_MESSAGES: Readonly<Record<FeedbackKind, string>> = Object.freeze({
  done: 'Marked done',
  dismiss: 'Dismissed',
  snooze: 'Snoozed',
});

/** The footer action, and the dialog title, that records a decision (AAP 0.5.5). */
export const RECORD_DECISION_LABEL = 'Record decision';

/** The status line a card shows after its decision form saved a decision. */
export const DECISION_RECORDED_MESSAGE = 'Decision recorded';

/** The text of a held card's link to the Campaigns discrepancy group (AAP 0.5.5). */
export const BLOCKING_DISCREPANCY_LINK_LABEL = 'View blocking discrepancy';

/**
 * The hold reason precedence P1 writes ("Resolve discrepancy first", AAP 0.5.4.2), used only
 * when a held item arrives without a reason of its own, so a held card never shows an empty
 * line.
 */
const HELD_REASON_FALLBACK = 'Resolve discrepancy first';

/** The provenance text when the frozen evidence names no dataset. */
const REPLACED_WITHOUT_PROVENANCE = 'Source data has since been replaced';

/** The error text when a failed feedback request carries no message of its own. */
const FEEDBACK_ERROR_FALLBACK = 'The action could not be recorded. Try again.';

/** The separator between the entity name and its source on the header's entity line. */
const META_SEPARATOR = ' · ';

// ---------------------------------------------------------------------------------------------
// Text helpers
// ---------------------------------------------------------------------------------------------

/** Trimmed text, or `null` for a missing or blank value, so "null" is never rendered. */
function textOrNull(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** "simulated" → "Simulated": the first letter of a mode in upper case. */
function capitalise(text: string): string {
  return text === '' ? text : `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

/**
 * Describes the data a historical item was computed from, beside its "Computed from replaced
 * data" badge (AAP 0.5.5), for example "Simulated LinkedIn data, since replaced by live".
 *
 * One phrase is written per distinct (mode, source) pair of the frozen provenance, in the order
 * the evidence lists them, as "{Mode} {source} data", and the phrases are joined with " and ".
 * The current data status follows as ", since replaced by {dataStatusNow}". The provenance is
 * the frozen record of what the run read, never the current status (AAP 0.3.2), which is why
 * the two are stated apart.
 *
 * @param provenance - the item's `evidence.provenance`, frozen at compute time
 * @param dataStatusNow - the item's `current.data_status_now`, read at request time
 * @returns the provenance sentence; "Source data has since been replaced" when the provenance
 *   is empty
 *
 * @example
 * ```ts
 * historicalProvenanceText(
 *   [{ source: 'linkedin', dataset_id: 7, mode: 'simulated', role: 'primary' }],
 *   'live',
 * ); // "Simulated LinkedIn data, since replaced by live"
 * historicalProvenanceText([], 'live'); // "Source data has since been replaced"
 * ```
 */
export function historicalProvenanceText(
  provenance: readonly ProvenanceEntry[],
  dataStatusNow: DataStatus,
): string {
  const phrases: string[] = [];
  const seen = new Set<string>();
  for (const entry of provenance) {
    const mode = textOrNull(entry.mode) ?? '';
    const source = textOrNull(entry.source);
    const key = JSON.stringify([mode, source]);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    // sourceLabel returns an unknown source string unchanged, so a later source still reads.
    const sourceText = source === null ? '' : sourceLabel(entry.source);
    phrases.push([capitalise(mode), sourceText, 'data'].filter((part) => part !== '').join(' '));
  }
  if (phrases.length === 0) {
    return REPLACED_WITHOUT_PROVENANCE;
  }
  const now = textOrNull(dataStatusNow);
  const replaced = now === null ? ', since replaced' : `, since replaced by ${now}`;
  return `${phrases.join(' and ')}${replaced}`;
}

/**
 * The header's entity line, "{entity name} · {Source}", leaving out a part the stored entity
 * does not carry.
 *
 * @param entity - the item's entity
 * @returns the line, or `undefined` when neither part is known, so no empty line renders
 */
function entityLine(entity: EntityView): string | undefined {
  const name = textOrNull(entity.name);
  // A null (or, at runtime, blank) source is left out rather than rendered as text.
  const source = entity.source ? sourceLabel(entity.source) : null;
  const line = [name, source]
    .filter((part): part is string => part !== null)
    .join(META_SEPARATOR);
  return line === '' ? undefined : line;
}

/**
 * The message a failed feedback request shows (AAP 0.5.3 status codes):
 *
 * - 409 `subject_retired`: {@link HISTORICAL_LABEL}, the same message as the historical badge,
 *   because the item was computed from data that has since been replaced (AAP 0.3.2);
 * - 422 (for example `feedback_not_allowed`) and every other failure: the API's own message,
 *   which the client guarantees is safe to show, or a generic retry text when it is blank.
 *
 * @param error - what the mutation rejected with
 * @returns the text for the footer's warning banner
 */
function feedbackErrorMessage(error: unknown): string {
  if (isRetiredError(error)) {
    return HISTORICAL_LABEL;
  }
  if (error instanceof Error) {
    return textOrNull(error.message) ?? FEEDBACK_ERROR_FALLBACK;
  }
  return FEEDBACK_ERROR_FALLBACK;
}

/**
 * Whether a failed request was refused because the item belongs to a replaced dataset.
 *
 * @param error - what the mutation rejected with
 * @returns `true` for a 409 `subject_retired` `ApiError`
 */
function isRetiredError(error: unknown): boolean {
  return isApiError(error) && error.status === 409 && error.code === 'subject_retired';
}

// ---------------------------------------------------------------------------------------------
// Footer actions
// ---------------------------------------------------------------------------------------------

/** Props of {@link RecommendationActions}. */
export interface RecommendationActionsProps {
  /** The item whose actions to render, as `GET /recommendations` returned it (with its overlay). */
  item: Item;
  /**
   * Overrides of the decision form's context, merged over {@link decisionContextFor}'s, for a
   * section that knows more than the evidence says (for example the members of a shared budget
   * read from its own rows). Omitted, the item's own evidence decides the prefill, the members
   * and the currency.
   */
  decisionContext?: Partial<DecisionContext>;
}

/** The feedback kinds in footer order, after the item's primary action. */
const SECONDARY_FEEDBACK: readonly FeedbackKind[] = ['dismiss', 'snooze'];

/**
 * A card's footer actions (AAP 0.5.5 "Footer actions"):
 *
 * - **Record decision** (primary) when the item has a `decision_subject`: it opens a `Dialog`
 *   titled "Record decision" holding a `DecisionForm` for that campaign or shared budget,
 *   prefilled as a patch with the item's proposed change (status `paused` for C1 and C1b; the
 *   proposed daily budget for B1, B2, B3 and C2 "Scale", written on the budget subject when the
 *   budget is shared, with every member listed). The form posts only the changed fields the
 *   subject's decision shape allows, and closes the dialog once the decision is saved.
 * - **Mark done** on every other action: feedback `done`, which never writes a decision.
 * - **Dismiss** and **Snooze for 7 days** (quiet) on every item: feedback `dismiss` and
 *   `snooze`, posted as `POST /recommendations/{id}/feedback`.
 *
 * C0 discrepancies render nothing here (`null`): they are exempt from feedback, and the card
 * renders their resolver instead (AAP 0.5.4.2, 0.5.4.4).
 *
 * Every action is disabled on a historical item (`current.historical`) and, from then on, after
 * a 409 `subject_retired`, which shows {@link HISTORICAL_LABEL}. While a feedback request is
 * pending, its button shows the loading state and every action is disabled; once it succeeds,
 * the actions stay disabled and a status line ("Marked done", "Dismissed", "Snoozed") stands
 * until the refetched recommendations hide the item. Any other failure shows the API's message
 * in a warning `Banner`, which takes focus so keyboard and screen-reader users land on it;
 * `role="alert"` stays reserved for the discrepancy banner (AAP 0.5.5 Behaviour).
 *
 * @param props - see {@link RecommendationActionsProps}
 * @returns the actions, or `null` for a C0 item
 *
 * @example
 * ```tsx
 * <Card.Footer>
 *   <RecommendationActions item={item} />
 * </Card.Footer>
 * ```
 */
export function RecommendationActions(props: RecommendationActionsProps): ReactElement | null {
  if (props.item.rule_id === 'C0') {
    return null;
  }
  // Keyed by the item, so a retired lock, a recorded feedback and an open dialog belong to the
  // item they were given on and never carry over to another item rendered in the same place.
  return <RecommendationActionsBody key={props.item.id} {...props} />;
}

/**
 * The footer actions of one non-C0 item; see {@link RecommendationActions}. Its state lives as
 * long as the item's id, because {@link RecommendationActions} keys it by that id.
 *
 * @param props - see {@link RecommendationActionsProps}
 * @returns the action row with its status and error lines, and the decision dialog
 */
function RecommendationActionsBody({ item, decisionContext }: RecommendationActionsProps) {
  const feedback = useFeedback();
  const [retired, setRetired] = useState(false);
  const [recorded, setRecorded] = useState<FeedbackKind | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [decisionSaved, setDecisionSaved] = useState(false);
  const errorRef = useRef<HTMLDivElement>(null);

  // A pressed button is disabled while its request runs, which drops keyboard focus to the
  // page. When the request fails, focus moves to the message instead (WCAG 2.2 SC 2.4.3, 3.3.1).
  useEffect(() => {
    if (errorText !== null) {
      errorRef.current?.focus();
    }
  }, [errorText]);

  const locked = item.current.historical || retired;
  const pending = feedback.isPending;
  const pendingKind = pending ? feedback.variables?.body.kind : undefined;
  // A recorded feedback ends the card's actions: a second post would only repeat it.
  const actionsDisabled = locked || pending || recorded !== null;

  const sendFeedback = (kind: FeedbackKind) => {
    if (actionsDisabled) {
      return;
    }
    setErrorText(null);
    setDecisionSaved(false);
    feedback.mutate(
      { recommendationId: item.id, body: { kind } },
      {
        onSuccess: () => {
          setRecorded(kind);
        },
        onError: (error) => {
          if (isRetiredError(error)) {
            setRetired(true);
          }
          setErrorText(feedbackErrorMessage(error));
        },
      },
    );
  };

  const feedbackButton = (kind: FeedbackKind, variant: 'secondary' | 'quiet') => (
    <Button
      key={kind}
      variant={variant}
      size="sm"
      loading={pendingKind === kind}
      disabled={actionsDisabled}
      onClick={() => {
        sendFeedback(kind);
      }}
    >
      {FEEDBACK_LABELS[kind]}
    </Button>
  );

  const subject = item.decision_subject;
  const closeDialog = () => {
    setDialogOpen(false);
  };

  let decisionDialog: ReactNode = null;
  if (subject !== null && subject !== undefined) {
    // The item's own evidence decides the prefill, members and currency; the caller's context
    // refines it.
    const context: DecisionContext = { ...decisionContextFor(item), ...decisionContext };
    decisionDialog = (
      <Dialog open={dialogOpen} title={RECORD_DECISION_LABEL} onClose={closeDialog}>
        <DecisionForm
          subject={subject}
          recommendationId={item.id}
          prefill={context.prefill}
          members={context.members}
          currency={context.currency}
          onDone={() => {
            setDialogOpen(false);
            setDecisionSaved(true);
          }}
          onCancel={closeDialog}
          disabled={locked}
        />
      </Dialog>
    );
  }

  const primaryAction =
    subject !== null && subject !== undefined ? (
      <Button
        variant="primary"
        size="sm"
        disabled={actionsDisabled}
        aria-haspopup="dialog"
        onClick={() => {
          setErrorText(null);
          setDecisionSaved(false);
          setDialogOpen(true);
        }}
      >
        {RECORD_DECISION_LABEL}
      </Button>
    ) : (
      feedbackButton('done', 'secondary')
    );

  const statusText =
    recorded !== null
      ? FEEDBACK_STATUS_MESSAGES[recorded]
      : decisionSaved
        ? DECISION_RECORDED_MESSAGE
        : null;

  return (
    <Stack gap={2}>
      <Inline gap={2}>
        {primaryAction}
        {SECONDARY_FEEDBACK.map((kind) => feedbackButton(kind, 'quiet'))}
      </Inline>
      {errorText === null ? null : (
        // Unstyled focus target for the message after a failed request; the Banner carries no
        // live-region role (AAP 0.5.5 reserves role="alert" for discrepancies).
        <div ref={errorRef} tabIndex={-1}>
          <Banner tone="warning" title={errorText} />
        </div>
      )}
      {/*
        A polite status region (WCAG 2.2 SC 4.1.3), rendered from the start so assistive
        technology announces the text when it appears; while empty it adds no Stack gap.
      */}
      <div role="status">{statusText}</div>
      {decisionDialog}
    </Stack>
  );
}


// ---------------------------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------------------------

/** Props of {@link RecommendationCard}. */
export interface RecommendationCardProps {
  /** The item to render, as `GET /recommendations` returned it (with its read-time overlay). */
  item: Item;
  /**
   * The section-specific fields (AAP 0.5.5 field sets), rendered after the reason sentence and
   * before the evidence table: for example a keyword card's term, spend, conversions,
   * cost/conversion and reason fields, or a budget card's utilization beside its efficiency.
   */
  children?: ReactNode;
  /**
   * Renders a C0 discrepancy's resolver in the footer, in place of the feedback actions C0 is
   * exempt from (AAP 0.5.4.4). It receives `disabled: true` on a historical item, so the
   * resolver is disabled like every other action. Ignored for every other rule.
   */
  resolver?: (state: { disabled: boolean }) => ReactNode;
  /**
   * Overrides of the "Record decision" form's context, passed to
   * {@link RecommendationActions}.
   */
  decisionContext?: Partial<DecisionContext>;
  /**
   * The level of the card's headline: `3` (the default) below a section's `<h2>`, `4` inside a
   * group headed by an `<h3>`, so no heading level is skipped.
   */
  headingLevel?: 3 | 4;
}

/**
 * Whether a slot value renders anything: `undefined`, `null`, booleans and the empty string
 * render nothing, so a resolver that returns nothing leaves no empty footer behind.
 */
function hasContent(node: ReactNode): boolean {
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * One recommendation card: "do this because of this" (AAP 0.5.5, 0.1.2). See the module
 * overview for its layout.
 *
 * The card is an `<article>` with the id `item-{id}`, named by its headline (the action), so
 * each card is announced by what it recommends and can be linked to. Badges sit in `Inline`
 * rows, so each chip keeps its own width instead of stretching across the column.
 *
 * @param props - see {@link RecommendationCardProps}
 * @returns the card
 *
 * @example
 * ```tsx
 * <RecommendationCard item={item} headingLevel={4}>
 *   <FieldList fields={keywordFieldList(item)} label="Keyword details" />
 * </RecommendationCard>
 *
 * <RecommendationCard
 *   item={c0Item}
 *   resolver={({ disabled }) => (
 *     <DiscrepancyResolution discrepancy={discrepancy} disabled={disabled} />
 *   )}
 * />
 * ```
 */
export function RecommendationCard({
  item,
  children,
  resolver,
  decisionContext,
  headingLevel,
}: RecommendationCardProps): ReactElement {
  const isC0 = item.rule_id === 'C0';
  const historical = item.current.historical;
  // The hold notice names a blocking discrepancy, so it shows only for a discrepancy hold:
  // - C0 is exempt from holds (AAP 0.5.4.2 P1), so a discrepancy card never shows one;
  // - a historical item is held by the overlay with "Computed from data that has since been
  //   replaced" (AAP 0.3.2), not by a discrepancy. Its badge and provenance already say so, and
  //   a link to the discrepancy group would point at nothing that blocks it.
  const held = !isC0 && !historical && isHeld(item);
  // The live reason first, then the one frozen with the run.
  const heldReason = held
    ? (textOrNull(item.current.held_reason_now) ??
      textOrNull(item.held_reason) ??
      HELD_REASON_FALLBACK)
    : null;
  const reason = textOrNull(item.reason);
  const provenanceText = historical
    ? historicalProvenanceText(item.evidence.provenance, item.current.data_status_now)
    : null;

  let footer: ReactNode;
  if (isC0) {
    footer = resolver?.({ disabled: historical });
  } else {
    footer = <RecommendationActions item={item} decisionContext={decisionContext} />;
  }

  return (
    <Card id={`item-${item.id}`}>
      <Card.Header
        title={item.action}
        rank={String(item.rank)}
        meta={entityLine(item.entity)}
        headingLevel={headingLevel ?? 3}
      />
      <Card.Body>
        {/*
          BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive, and feature code
          carries no CSS (AAP 0.4.4). Text lines are therefore <div> and <span> elements placed
          in the layout primitives rather than <p>, whose user-agent margins would add space no
          spacing token sets; the Stack and Inline gap tokens alone space every line.
        */}
        <Stack gap={3}>
          {provenanceText === null ? null : (
            <Inline gap={2}>
              <Badge tone="warning">{HISTORICAL_LABEL}</Badge>
              <span>{provenanceText}</span>
            </Inline>
          )}
          {heldReason === null ? null : (
            <Inline gap={2}>
              <span>{heldReason}</span>
              {/* An in-page link to the Campaigns discrepancy group (AAP 0.5.5). */}
              <a href={`#${DISCREPANCY_GROUP_ANCHOR}`}>{BLOCKING_DISCREPANCY_LINK_LABEL}</a>
            </Inline>
          )}
          {reason === null ? null : <div>{reason}</div>}
          {children}
          <EvidenceTable evidence={item.evidence} />
          <Inline>
            <Badge tone="neutral">{formatWindowChip(item.evidence.window)}</Badge>
          </Inline>
          <FlagList flags={item.flags} current={item.current.flags_now} />
        </Stack>
      </Card.Body>
      {hasContent(footer) ? <Card.Footer>{footer}</Card.Footer> : null}
    </Card>
  );
}

