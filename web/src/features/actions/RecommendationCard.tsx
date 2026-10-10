/**
 * `RecommendationCard`: one "do this because of this" action on `/actions` (AAP 0.5.5
 * Recommendation card; AAP 0.1.2: the card leads with the action, and every number sits beside
 * the claim it supports).
 *
 * Layout, top to bottom:
 *
 * 1. **Header** (`Card.Header`): the rank, the action as the headline (the card's accessible
 *    name), the headline number, and the entity name with its source, for example "Generic
 *    CRM — US · Google". The headline number is the metric the rule selected in the frozen
 *    evidence (`extra.headline_metric`; K1's is the wasted spend), shown as "{label} {value}"
 *    with its unit and currency, so the card leads with the number that justifies the action
 *    (AAP 0.1.2). The card never computes or picks the number itself: evidence that selects
 *    none, such as a run frozen before the key existed, shows no headline number.
 * 2. **Body** (`Card.Body`):
 *    - for a historical item (`current.historical`), the "Computed from replaced data" badge and
 *      the original provenance, for example "Simulated LinkedIn data, since replaced by live"
 *      (AAP 0.3.2: the frozen provenance and the current status are shown apart);
 *    - for an item held by a discrepancy, why it is held ("Resolve discrepancy first") and a
 *      link to each discrepancy that holds it ({@link BlockingDiscrepancyLinks}: the run's C0
 *      card or the discrepancy on `/decisions`, named by subject and kind), or "View open
 *      discrepancies" to the Campaigns discrepancy group when no blocker can be named
 *      (AAP 0.5.4.2 P1, 0.5.5 "with the blocking discrepancy linked"). C0 items are exempt from
 *      holds and never show it, and neither do historical items, whose overlay hold is the
 *      replaced data the badge already states;
 *    - the reason sentence, then the section-specific fields the caller passes as `children`.
 *      A section whose own fields list the reason in a fixed position passes
 *      `reasonPlacement="fields"`, and the card prints no reason line of its own, so the reason
 *      appears once: the keyword cards, whose five fields end with it (AAP 0.1.2: term, spend,
 *      conversions, cost/conversion, reason);
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
 * went stale before its refetch, whether a feedback or the decision form received it, always
 * shows the same message, "Computed from replaced data", as the card's failure text, which stays
 * until the next action starts. It locks the actions the same way only while the recommendation
 * the refused request submitted is the item shown, and the lock belongs to that run's item: a
 * newer run's item is judged by its own `current.historical`. A decision save keeps every
 * footer action disabled until it settles, even once its dialog is closed, and its failure is
 * shown on the card. The rest of the footer's state belongs to the recommendation, not to one
 * run's item, so a newer run's item of the same recommendation keeps the footer's buttons,
 * keyboard focus on them, the failure text and an open decision dialog (WCAG 2.2 SC 2.4.3).
 *
 * A recorded feedback can remove its own card: the refetched item carries `current.hidden_by`
 * and its section drops it. The outcome is therefore announced in the polite region of an
 * {@link ActionOutcomeProvider} above the cards, which outlives them (WCAG 2.2 SC 4.1.3), and
 * keyboard focus, which the disabled or removed button lost, moves to the next card's first
 * enabled action, else the previous card's, else the section heading (WCAG 2.2 SC 2.4.3).
 *
 * Every text on the card (action, headline metric label, reason, flag messages, held reason,
 * entity name) comes from the API, so no threshold, window length or rule wording is written
 * into the client (AAP 0.5.4.1). The flags row adds only each code's fixed AAP 0.5.5 label
 * (`FLAG_LABELS` in `FlagList.tsx`) before a message that does not name it, and that label
 * carries no threshold or window length. Server state goes only through `../../api/queries`
 * (AAP 0.11.1), and UI comes only from the design-system index (AAP 0.4.4): no raw buttons,
 * headings or tables, no inline styles and no stylesheet; layout comes from `Stack` and `Inline`.
 *
 * @packageDocumentation
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import type { ReactElement, ReactNode } from 'react';

import { isApiError, useFeedback } from '../../api/queries';
import type {
  DataStatus,
  DecisionSubjectRef,
  EntityView,
  FeedbackKind,
  Item,
  ProvenanceEntry,
} from '../../api/types';
import { Badge, Banner, Button, Card, Dialog, Inline, Stack } from '../../design-system';
import type { CardHeaderProps } from '../../design-system';
import { formatMetric, formatWindowChip, sourceLabel } from '../../lib/format';
import { DecisionForm } from '../decisions/DecisionForm';
import { BlockingDiscrepancyLinks } from './BlockingDiscrepancies';
import { EvidenceTable } from './EvidenceTable';
import { decisionContextFor, headlineMetric, type DecisionContext } from './evidenceExtra';
import { FlagList } from './FlagList';
import { isHeld, itemIdentity } from './sections/SectionFrame';

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

/**
 * The status line a card shows while a decision save runs with its dialog closed, until the
 * save settles.
 */
export const DECISION_SAVING_MESSAGE = 'Saving decision';

/**
 * The text a held card's link to a specific blocking discrepancy starts with (AAP 0.5.5),
 * defined beside the links in `./BlockingDiscrepancies` and re-exported for the card's callers.
 */
export { BLOCKING_DISCREPANCY_LINK_LABEL } from './BlockingDiscrepancies';

/**
 * The hold reason precedence P1 writes ("Resolve discrepancy first", AAP 0.5.4.2), used only
 * when a held item arrives without a reason of its own, so a held card never shows an empty
 * line.
 */
const HELD_REASON_FALLBACK = 'Resolve discrepancy first';

/** The provenance text when the frozen evidence names no dataset. */
const REPLACED_WITHOUT_PROVENANCE = 'Source data has since been replaced';

/**
 * The error text when a failed feedback request carries no API message of its own: a blank
 * `ApiError` message, or a rejection that is not an `ApiError`.
 */
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
 * The header's headline number (AAP 0.1.2): the metric the item's rule selected in its frozen
 * evidence ({@link headlineMetric}), labelled with the API's own metric label and formatted by
 * `formatMetric` with its unit and currency, so an unavailable value reads "unavailable", never
 * `0`, and a floor keeps its "≥" or "≤" mark.
 *
 * @param item - the recommendation
 * @returns the figure, or `undefined` when the evidence selects no headline metric
 */
function headlineFigure(item: Item): CardHeaderProps['figure'] {
  const metric = headlineMetric(item);
  return metric === null ? undefined : { label: metric.label, value: formatMetric(metric) };
}

/**
 * The message a failed feedback request shows (AAP 0.5.3 status codes), for every failure but a
 * 409 `subject_retired`, which always shows {@link HISTORICAL_LABEL} instead, never the generic
 * retry text, whether or not it locks the item shown:
 *
 * - 422 (for example `feedback_not_allowed`) and every other `ApiError`: its message, which is
 *   the API's envelope message or the client's own fallback text, both safe to show, or a
 *   generic retry text when it is blank;
 * - any other rejection, such as a plain `Error`, a `TypeError` or an abort: the generic retry
 *   text, because its message is outside that contract and may carry internal detail.
 *
 * @param error - what the mutation rejected with
 * @returns the text for the footer's warning banner
 */
function feedbackErrorMessage(error: unknown): string {
  return (isApiError(error) ? textOrNull(error.message) : null) ?? FEEDBACK_ERROR_FALLBACK;
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

/**
 * The confirmation an outcome announces, naming the card it belongs to, because that card may
 * be gone by the time it is read: "{status}: {action} ({entity name})", for example
 * "Dismissed: Pause (Generic CRM — US)". A part the item does not carry is left out.
 *
 * @param status - the outcome, for example `FEEDBACK_STATUS_MESSAGES.dismiss` or
 *   {@link DECISION_RECORDED_MESSAGE}
 * @param item - the item the outcome was recorded on
 * @returns the message for {@link ActionOutcomeProvider}'s region
 */
function outcomeMessage(status: string, item: Item): string {
  const action = textOrNull(item.action);
  const name = textOrNull(item.entity.name);
  if (action === null) {
    return name === null ? status : `${status}: ${name}`;
  }
  return name === null ? `${status}: ${action}` : `${status}: ${action} (${name})`;
}

// ---------------------------------------------------------------------------------------------
// Outcome announcements
// ---------------------------------------------------------------------------------------------

/**
 * Announces one outcome in the polite region of the nearest {@link ActionOutcomeProvider}.
 * Blank text is ignored.
 *
 * @param message - the confirmation, for example "Marked done: Add "crm demo" as exact-match
 *   negative (Generic CRM — US)"
 */
export type AnnounceActionOutcome = (message: string) => void;

/** The announcer of the nearest {@link ActionOutcomeProvider}; `null` outside one. */
const ActionOutcomeContext = createContext<AnnounceActionOutcome | null>(null);

/** The latest announced outcome. */
interface ActionOutcome {
  /** The text announced. */
  message: string;
  /** Increases with every announcement, so each one renders as a new node. */
  sequence: number;
}

/** Props of {@link ActionOutcomeProvider}. */
export interface ActionOutcomeProviderProps {
  /** The content whose card actions announce their outcomes here: on `/actions`, the sections. */
  children?: ReactNode;
}

/**
 * A polite live region for the outcomes of the card actions inside it (WCAG 2.2 SC 4.1.3),
 * placed above those cards so that it outlives every one of them.
 *
 * A recorded feedback can unmount the card that recorded it: the refetched recommendations
 * carry `current.hidden_by`, and the section drops the item. A status line inside that card
 * would leave with it, unread. Every {@link RecommendationActions} inside this provider
 * therefore announces "Marked done", "Dismissed", "Snoozed" or "Decision recorded", with the
 * action and the entity name, here; its own status line stays visible but is no second live
 * region, so nothing is announced twice.
 *
 * The region is a `<div role="status">` rendered before the children from the first render, so
 * every message lands in a region that already exists. It is never `role="alert"`, which
 * AAP 0.5.5 reserves for the discrepancy banner. It stays empty until the first outcome, and a
 * `Stack` takes an empty status child out of its column, so it adds no space until then. Each
 * message is a new child node, so the same text twice in a row (two cards dismissed with the
 * same action on the same entity) is announced twice. The latest message stays until the next
 * one replaces it.
 *
 * The provider renders no element around its children: place it among the children of a
 * `Stack`, where the region and the children each take the column's gap.
 *
 * @param props - see {@link ActionOutcomeProviderProps}
 * @returns the live region, followed by the children
 *
 * @example
 * ```tsx
 * <Stack gap={6}>
 *   <SectionNav sections={sections} />
 *   <ActionOutcomeProvider>
 *     <KeywordsSection section={keywords} run={run} overlay={overlay} />
 *     <BudgetSection section={budget} run={run} overlay={overlay} />
 *   </ActionOutcomeProvider>
 * </Stack>
 * ```
 */
export function ActionOutcomeProvider({ children }: ActionOutcomeProviderProps): ReactElement {
  const [outcome, setOutcome] = useState<ActionOutcome | null>(null);
  // Stable for the provider's lifetime, so a card may call the announcer it captured from a
  // request that settles after the card itself has unmounted.
  const announce = useCallback<AnnounceActionOutcome>((message) => {
    const text = message.trim();
    if (text === '') {
      return;
    }
    setOutcome((previous) => ({ message: text, sequence: (previous?.sequence ?? 0) + 1 }));
  }, []);
  return (
    <ActionOutcomeContext value={announce}>
      <div role="status">
        {outcome === null ? null : <span key={outcome.sequence}>{outcome.message}</span>}
      </div>
      {children}
    </ActionOutcomeContext>
  );
}

/**
 * The announcer of the nearest {@link ActionOutcomeProvider}, which {@link RecommendationActions}
 * uses to confirm its outcomes outside the card.
 *
 * @returns the announcer, or `null` outside a provider, where each card keeps its own polite
 *   status line
 *
 * @example
 * ```tsx
 * const announce = useActionOutcome();
 * announce?.('Dismissed: Pause (Generic CRM — US)');
 * ```
 */
export function useActionOutcome(): AnnounceActionOutcome | null {
  return useContext(ActionOutcomeContext);
}

// ---------------------------------------------------------------------------------------------
// Focus recovery after a recorded feedback
// ---------------------------------------------------------------------------------------------

/**
 * Finds the action rows of the cards: {@link RecommendationActionsBody} marks its row root with
 * the attribute `data-recommendation-actions`.
 */
const ACTIONS_ROW_SELECTOR = '[data-recommendation-actions]';

/** Where a feedback action was pressed, captured on the click while its card is on the page. */
interface FocusOrigin {
  /** The pressed action's row. */
  row: HTMLElement;
  /** The row's ancestor `<section>` elements, nearest first. */
  sections: readonly HTMLElement[];
  /**
   * Every action row inside the outermost of those sections, or the whole document when there
   * is none, in document order.
   */
  rows: readonly HTMLElement[];
  /** The position of `row` in `rows`. */
  index: number;
}

/**
 * Captures where focus can go once a feedback is recorded: the pressed action's row, its
 * enclosing sections and the action rows around it.
 *
 * @param row - the pressed action's row, or `null` before it is attached
 * @returns the origin, or `null` without a row
 */
function captureFocusOrigin(row: HTMLElement | null): FocusOrigin | null {
  if (row === null) {
    return null;
  }
  const sections: HTMLElement[] = [];
  for (let node = row.parentElement; node !== null; node = node.parentElement) {
    if (node.localName === 'section') {
      sections.push(node);
    }
  }
  const scope: ParentNode = sections.at(-1) ?? row.ownerDocument;
  const rows = Array.from(scope.querySelectorAll<HTMLElement>(ACTIONS_ROW_SELECTOR));
  return { row, sections, rows, index: rows.indexOf(row) };
}

/**
 * Whether keyboard focus is lost or still inside `scope`: on no element, on the body (where a
 * browser leaves it once the focused button is disabled or removed), on an element no longer in
 * the document, or inside `scope`. Anywhere else the user has moved focus, and it stays there.
 *
 * @param scope - the element focus counts as still being in, such as a card's action row
 * @returns `true` when focus may be moved without taking it from where the user put it
 */
function focusIsLostOrWithin(scope: Element): boolean {
  const { activeElement, body } = scope.ownerDocument;
  return (
    activeElement === null ||
    activeElement === body ||
    !activeElement.isConnected ||
    scope.contains(activeElement)
  );
}

/**
 * The first action of a row that can take focus: an enabled button, neither `disabled` nor
 * `aria-disabled="true"`, outside every `[hidden]` or `[inert]` subtree, such as a collapsed
 * held disclosure, an inactive tab panel or a collapsed table detail row.
 *
 * @param row - an action row
 * @returns the button, or `null` when the row offers none
 */
function firstEnabledAction(row: HTMLElement): HTMLButtonElement | null {
  const buttons = Array.from(row.querySelectorAll('button'));
  return (
    buttons.find(
      (button) =>
        !button.disabled &&
        button.getAttribute('aria-disabled') !== 'true' &&
        button.closest('[hidden], [inert]') === null,
    ) ?? null
  );
}

/**
 * The heading that names a section: the element its `aria-labelledby` points at.
 * `PageShell.Section` labels each section by its `${id}-title` heading.
 *
 * @param section - a `<section>` element
 * @returns the heading, or `null` when the section names none on the page
 */
function sectionHeading(section: HTMLElement): HTMLElement | null {
  const [id] = (section.getAttribute('aria-labelledby') ?? '').split(/\s+/u).filter(Boolean);
  return id === undefined ? null : section.ownerDocument.getElementById(id);
}

/**
 * Focuses `element`. With `fromScript`, an element without a `tabindex` of its own (a heading,
 * `<main>`) first gets `tabindex="-1"`, which lets script focus it without adding a tab stop.
 *
 * @param element - the element to focus
 * @param fromScript - whether the element may need `tabindex="-1"` to take focus
 * @returns whether focus is now on `element`: a browser refuses focus to an element that is not
 *   rendered, which sends recovery on to the next candidate
 */
function moveFocusTo(element: HTMLElement, fromScript = false): boolean {
  if (fromScript && !element.hasAttribute('tabindex')) {
    element.setAttribute('tabindex', '-1');
  }
  element.focus();
  return element.ownerDocument.activeElement === element;
}

/**
 * Gives keyboard focus a stable place after a recorded feedback has disabled the action that
 * had it, and often removed its card (WCAG 2.2 SC 2.4.3): the first enabled action of the next
 * card row still on the page, else of the previous rows, nearest first, else the heading of
 * the nearest enclosing section still on the page, else the page's `<main>`. Rows are looked
 * for inside the outermost enclosing section only, so focus stays in the section the user was
 * working in.
 *
 * Focus the user has moved away from the card in the meantime is left where it is
 * ({@link focusIsLostOrWithin}).
 *
 * @param origin - what {@link captureFocusOrigin} captured on the click
 */
function recoverFocus(origin: FocusOrigin): void {
  const { row, sections, rows, index } = origin;
  if (!focusIsLostOrWithin(row)) {
    return;
  }
  const following = rows.slice(index + 1);
  const preceding = rows.slice(0, Math.max(index, 0)).reverse();
  for (const candidate of [...following, ...preceding]) {
    if (candidate === row || !candidate.isConnected) {
      continue;
    }
    const action = firstEnabledAction(candidate);
    if (action !== null && moveFocusTo(action)) {
      return;
    }
  }
  for (const section of sections) {
    const heading = section.isConnected ? sectionHeading(section) : null;
    if (heading !== null && moveFocusTo(heading, true)) {
      return;
    }
  }
  const main = row.ownerDocument.querySelector('main');
  if (main !== null) {
    moveFocusTo(main, true);
  }
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

/** The item a "Record decision" dialog was opened from. */
interface DialogOpener {
  /** The item's id, which the dialog's form posts as `recommendation_id`. */
  readonly recommendationId: Item['id'];
  /** The item's decision subject, which the form records about. */
  readonly subject: DecisionSubjectRef | null;
}

/**
 * The "Record decision" dialog of one card: whether it is open, which opening it is, and the
 * item it was opened from. `session` grows with every opening, so a save that ends after its
 * dialog was closed can tell whether the dialog shown now is still its own.
 */
interface DialogSession {
  /** Whether the dialog is shown. */
  readonly open: boolean;
  /** The number of the latest opening; `0` before the first. */
  readonly session: number;
  /**
   * The item the latest opening was made from; `null` before the first. While the dialog stays
   * open and the decision subject is unchanged, the form keeps posting this item's id, so a
   * newer run's item of the same recommendation keeps the open form, its edits and focus
   * included (the form starts afresh whenever the recommendation id it is given changes). The
   * API accepts any recommendation whose subject matches the decision's.
   */
  readonly opener: DialogOpener | null;
}

/** The dialog before its first opening. */
const CLOSED_DIALOG: DialogSession = { open: false, session: 0, opener: null };

/** The dialog opened anew from `item`: a session no earlier save belongs to. */
function openDialogSession(current: DialogSession, item: Item): DialogSession {
  return {
    open: true,
    session: current.session + 1,
    opener: { recommendationId: item.id, subject: item.decision_subject ?? null },
  };
}

/** Whether two decision subjects are the same campaign or budget. */
function sameSubject(
  left: DecisionSubjectRef | null,
  right: DecisionSubjectRef | null | undefined,
): boolean {
  return (
    left !== null &&
    right !== null &&
    right !== undefined &&
    left.type === right.type &&
    left.id === right.id
  );
}

/** The dialog closed, keeping its session; unchanged when it is closed already. */
function closeDialogSession(current: DialogSession): DialogSession {
  return current.open ? { ...current, open: false } : current;
}

/**
 * A card's footer actions (AAP 0.5.5 "Footer actions"):
 *
 * - **Record decision** (primary) when the item has a `decision_subject`: it opens a `Dialog`
 *   titled "Record decision" holding a `DecisionForm` for that campaign or shared budget,
 *   prefilled as a patch with the item's proposed change (status `paused` for C1 and C1b; the
 *   proposed daily budget for B1, B2, B3 and C2 "Scale", written on the budget subject when the
 *   budget is shared, with every member listed). The form posts only the changed fields the
 *   subject's decision shape allows, and closes the dialog once the decision is saved. The
 *   dialog can be closed while the save runs (Cancel, Close, Escape or the backdrop); until the
 *   save settles, every action stays disabled and "Record decision" shows its loading state, so
 *   the dialog cannot be reopened and no second decision can be posted, and with the dialog
 *   closed the card's status line reads "Saving decision". A save that ends after its dialog
 *   was closed is still reported: a success as "Decision recorded", a failure in the warning
 *   `Banner`. Focus, which the closed dialog could not return to the disabled button, then
 *   goes back to "Record decision" when nothing else has taken it.
 * - **Mark done** on every other action: feedback `done`, which never writes a decision.
 * - **Dismiss** and **Snooze for 7 days** (quiet) on every item: feedback `dismiss` and
 *   `snooze`, posted as `POST /recommendations/{id}/feedback`.
 *
 * C0 discrepancies render nothing here (`null`): they are exempt from feedback, and the card
 * renders their resolver instead (AAP 0.5.4.2, 0.5.4.4).
 *
 * Every action is disabled on a historical item (`current.historical`). A 409
 * `subject_retired` to a feedback or to the decision form (its `onRetired`) disables them the
 * same way, including after the dialog closes, while the recommendation the refused request
 * submitted (the feedback's item at the click, the id the form posted) is the item shown. While
 * a feedback request is pending, its button shows the loading state and every action is
 * disabled; once it succeeds, the actions stay disabled and a status line ("Marked done",
 * "Dismissed", "Snoozed") stands until the refetched recommendations hide the item. Inside an
 * {@link ActionOutcomeProvider} that outcome, and a recorded decision, is announced in the
 * provider's region, which outlives the card, instead of in the card's own line. Keyboard
 * focus, lost with the disabled or removed button, then moves to the next card's first enabled
 * action, else the previous card's, else the enclosing section's heading, else `<main>`, unless
 * the user has moved it elsewhere (WCAG 2.2 SC 2.4.3). Every refused action, of a feedback or
 * of a decision save, shows its explanation in a warning `Banner` until the next action starts:
 * {@link HISTORICAL_LABEL} for every 409 `subject_retired`, whether or not it locked the item
 * shown; for any other failure (the form's `onFailed`), the API's message, or a generic text
 * for a failure that carries none. The banner takes focus when it appears, or once the
 * decision dialog closes, so keyboard and screen-reader users land on it; `role="alert"` stays
 * reserved for the discrepancy banner (AAP 0.5.5 Behaviour).
 *
 * The actions' state belongs to the recommendation ({@link itemIdentity}), not to one run's
 * item: when a newer run's item of the same recommendation takes the item's place, the buttons
 * keep their elements, so keyboard focus stays on the one that had it (WCAG 2.2 SC 2.4.3), and
 * a recorded feedback, a failure's text with its banner and the focus on it ("Computed from
 * replaced data" included), "Decision recorded", an open dialog with its form and a running
 * decision save carry over. An open dialog closes when the newer item has no decision subject,
 * and focus moves to its first enabled action, else to the card. Only the `subject_retired`
 * lock belongs to the run's item it was learned on, the recommendation id the refused request
 * submitted: the newer item is judged by its own `current.historical`, and a refusal for an
 * item already replaced never locks the item shown. A different recommendation rendered in the
 * same place starts with fresh state.
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
  // Keyed by the recommendation, never by one run's item id: a newer run's item of the same
  // recommendation keeps the buttons, and focus on them, while another recommendation rendered
  // in the same place never inherits this one's state.
  return <RecommendationActionsBody key={itemIdentity(props.item)} {...props} />;
}

/**
 * The footer actions of one non-C0 item; see {@link RecommendationActions}. Its state lives as
 * long as the item's recommendation identity ({@link itemIdentity}), because
 * {@link RecommendationActions} keys it by that identity, so it outlives a newer run's item of
 * the same recommendation. The one exception is the `subject_retired` lock: it belongs to the
 * run's item it was learned on, the recommendation id the refused request submitted, so it is
 * lifted when the item's id changes and is never set by a refusal for an item already replaced.
 * The "Computed from replaced data" explanation of every refusal is failure text like any
 * other, and stays until the next action starts.
 *
 * @param props - see {@link RecommendationActionsProps}
 * @returns the action row with its status and error lines, and the decision dialog
 */
function RecommendationActionsBody({ item, decisionContext }: RecommendationActionsProps) {
  const feedback = useFeedback();
  const announce = useActionOutcome();
  const [retired, setRetired] = useState(false);
  const [recorded, setRecorded] = useState<FeedbackKind | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogSession>(CLOSED_DIALOG);
  const dialogOpen = dialog.open;
  const [decisionSaved, setDecisionSaved] = useState(false);
  // Whether a decision save is running. The form reports it even once its dialog was closed,
  // so the card keeps every action disabled until the save settles: a reopened dialog would
  // start a fresh form, and with it a second, overlapping save.
  const [decisionPending, setDecisionPending] = useState(false);
  // Set when a decision save settles, and consumed by the first commit after it.
  const [refocusOpener, setRefocusOpener] = useState(false);
  const rowRef = useRef<HTMLDivElement>(null);
  const messageRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  // The id of the item the row showed at its latest commit. A retirement locks the row only
  // while the recommendation its request submitted is still the item shown
  // ({@link learnRetirement}).
  const shownIdRef = useRef(item.id);
  // Set when an open dialog's item lost its decision subject; consumed by the first commit
  // after it.
  const [refocusRow, setRefocusRow] = useState(false);

  // A newer run's item of the same recommendation has taken the item's place, before the
  // browser paints. The lock belongs to the previous run's item: the new one is judged by its
  // own `current.historical`. Everything else carries over, the failure text with its banner and
  // the focus it holds included, but an open dialog needs a decision subject, so an item without
  // one closes it.
  useLayoutEffect(() => {
    if (shownIdRef.current === item.id) {
      return;
    }
    shownIdRef.current = item.id;
    setRetired(false);
    if (dialogOpen && (item.decision_subject === null || item.decision_subject === undefined)) {
      setDialog(closeDialogSession);
      setRefocusRow(true);
    }
  }, [item.id, item.decision_subject, dialogOpen]);

  // After the commit that closed the dialog, the element that held focus is gone. Focus moves
  // to the row's first enabled action, else to the card, so it stays on the item (WCAG 2.2
  // SC 2.4.3): only when focus was lost or is still on the row, never where the user has moved
  // it, and never over a message still shown, which takes focus itself (the effect below).
  useLayoutEffect(() => {
    if (!refocusRow) {
      return;
    }
    setRefocusRow(false);
    const row = rowRef.current;
    if (row === null || messageRef.current !== null || !focusIsLostOrWithin(row)) {
      return;
    }
    const action = firstEnabledAction(row);
    if (action !== null && moveFocusTo(action)) {
      return;
    }
    // Outside a card, such as a white-paper row's details, the row itself takes focus.
    moveFocusTo(row.closest('article') ?? row, true);
  }, [refocusRow]);

  /**
   * Records a 409 `subject_retired` to a request that submitted the recommendation `sentFor`:
   * a feedback's item id at the click, or the id the decision form posted. Every such refusal
   * shows {@link HISTORICAL_LABEL} as the card's failure text (AAP 0.5.5: "shown as the same
   * message"), which, like any failure text, stays until the next action starts, also across a
   * newer run's item. The lock is set only while `sentFor` is the item shown: a refusal for an
   * item a newer run has replaced since leaves the item shown now unlocked, judged by its own
   * `current.historical`, so the refused action may be tried again on it.
   */
  const learnRetirement = (sentFor: Item['id']) => {
    setErrorText(HISTORICAL_LABEL);
    if (shownIdRef.current === sentFor) {
      setRetired(true);
    }
  };

  // The failure text alone is the card's message, independent of the lock: a retirement's is
  // HISTORICAL_LABEL ({@link learnRetirement}); a failed feedback or decision save shows its own.
  const message = errorText;

  // A pressed button is disabled while its request runs, which drops keyboard focus to the
  // page. When the request fails, focus moves to the message instead (WCAG 2.2 SC 2.4.3, 3.3.1).
  // The banner is no live region, so taking focus is how its message is read. While the
  // decision dialog is open its focus trap owns focus, so the message waits and takes focus
  // once the dialog closes: React runs the dialog's cleanup, which returns focus to the
  // "Record decision" button (disabled when a retirement locked the row), before this effect.
  useEffect(() => {
    if (message !== null && !dialogOpen) {
      messageRef.current?.focus();
    }
  }, [message, dialogOpen]);

  // A dialog closed while its save runs cannot return focus to "Record decision", which stays
  // disabled until the save settles, so focus drops to the page. Once the save has settled and
  // the button is enabled again, focus goes back to it (WCAG 2.2 SC 2.4.3): only when focus is
  // still lost, never while the dialog is open and owns focus, and never over a message, which
  // takes focus itself (the effect above).
  useEffect(() => {
    if (!refocusOpener) {
      return;
    }
    setRefocusOpener(false);
    const opener = openerRef.current;
    const row = rowRef.current;
    if (dialogOpen || message !== null || opener === null || row === null || opener.disabled) {
      return;
    }
    if (focusIsLostOrWithin(row)) {
      opener.focus();
    }
  }, [refocusOpener, dialogOpen, message]);

  const locked = item.current.historical || retired;
  const pending = feedback.isPending;
  const pendingKind = pending ? feedback.variables?.body.kind : undefined;
  // A recorded feedback ends the card's actions: a second post would only repeat it. A running
  // decision save holds them until it settles.
  const actionsDisabled = locked || pending || recorded !== null || decisionPending;

  const sendFeedback = (kind: FeedbackKind) => {
    if (actionsDisabled) {
      return;
    }
    setErrorText(null);
    setDecisionSaved(false);
    // Captured on the click, while this card and its neighbours are on the page: a success can
    // remove this card before focus is recovered.
    const origin = captureFocusOrigin(rowRef.current);
    // mutateAsync, not mutate's per-call callbacks, which TanStack Query skips once this card
    // has unmounted: `useFeedback` settles only after the recommendations have been refetched,
    // and a refetched item carrying `current.hidden_by` removes the card.
    void feedback.mutateAsync({ recommendationId: item.id, body: { kind } }).then(
      () => {
        setRecorded(kind);
        announce?.(outcomeMessage(FEEDBACK_STATUS_MESSAGES[kind], item));
        if (origin !== null) {
          // This continuation runs before React commits the refetched page, because TanStack
          // Query notifies its observers from a zero-delay timer scheduled before it. A timer
          // scheduled now runs after that commit, once a hidden card has left the page.
          window.setTimeout(() => {
            recoverFocus(origin);
          }, 0);
        }
      },
      (error: unknown) => {
        // `item` is the item shown on the click, the one the request was sent for.
        if (isRetiredError(error)) {
          learnRetirement(item.id);
        } else {
          setErrorText(feedbackErrorMessage(error));
        }
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
    setDialog(closeDialogSession);
  };

  let decisionDialog: ReactNode = null;
  if (subject !== null && subject !== undefined) {
    // The item's own evidence decides the prefill, members and currency; the caller's context
    // refines it.
    const context: DecisionContext = { ...decisionContextFor(item), ...decisionContext };
    // The opening this render's form belongs to. DecisionForm calls the onDone it had when the
    // user saved, even after its dialog was closed, so a save that ends late closes only its
    // own opening and never a dialog opened since. No opening starts while a save runs
    // (`decisionPending`), and the decision is recorded either way, so the outcome is still
    // shown and announced.
    const formSession = dialog.session;
    // The recommendation the open form posts: the item the dialog was opened from, while the
    // subject is unchanged, so a newer run's item of the same recommendation keeps the form
    // with its edits and focus (DecisionForm starts afresh when this id changes); otherwise the
    // item shown now.
    const opener = dialog.opener;
    const formRecommendationId =
      dialogOpen && opener !== null && sameSubject(opener.subject, subject)
        ? opener.recommendationId
        : item.id;
    decisionDialog = (
      <Dialog open={dialogOpen} title={RECORD_DECISION_LABEL} onClose={closeDialog}>
        <DecisionForm
          subject={subject}
          recommendationId={formRecommendationId}
          prefill={context.prefill}
          members={context.members}
          currency={context.currency}
          onDone={() => {
            setDialog((current) =>
              current.session === formSession ? closeDialogSession(current) : current,
            );
            setDecisionSaved(true);
            announce?.(outcomeMessage(DECISION_RECORDED_MESSAGE, item));
          }}
          onRetired={() => {
            // The subject's dataset was replaced. The form calls the onRetired it had when the
            // user saved, so `formRecommendationId` is the recommendation that save posted: the
            // refusal is that recommendation's. While it is the item shown, every footer action
            // locks, not only the form, so closing and reopening the dialog cannot lift the lock.
            learnRetirement(formRecommendationId);
          }}
          onPendingChange={(saving) => {
            setDecisionPending(saving);
            if (saving) {
              // A new save: an earlier outcome no longer describes it.
              setErrorText(null);
              setDecisionSaved(false);
              setRefocusOpener(false);
            } else {
              setRefocusOpener(true);
            }
          }}
          onFailed={(failure) => {
            // Shown on the card as well as in the form, so a failure whose dialog was closed
            // mid-save, unmounting the form, is still shown, and takes focus once the dialog is
            // closed.
            setErrorText(failure);
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
        ref={openerRef}
        variant="primary"
        size="sm"
        loading={decisionPending}
        disabled={actionsDisabled}
        aria-haspopup="dialog"
        onClick={() => {
          if (actionsDisabled) {
            return;
          }
          setErrorText(null);
          setDecisionSaved(false);
          setDialog((current) => openDialogSession(current, item));
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
        : decisionPending && !dialogOpen
          ? DECISION_SAVING_MESSAGE
          : null;

  return (
    // The unstyled row root focus recovery finds the card's actions by (ACTIONS_ROW_SELECTOR);
    // the Stack inside it does all the layout.
    <div ref={rowRef} data-recommendation-actions="">
      <Stack gap={2}>
        <Inline gap={2}>
          {primaryAction}
          {SECONDARY_FEEDBACK.map((kind) => feedbackButton(kind, 'quiet'))}
        </Inline>
        {message === null ? null : (
          // Unstyled focus target for the message after a failed request; the Banner carries no
          // live-region role (AAP 0.5.5 reserves role="alert" for discrepancies).
          <div ref={messageRef} tabIndex={-1}>
            <Banner tone="warning" title={message} />
          </div>
        )}
        {announce === null ? (
          // Outside an ActionOutcomeProvider: a polite status region (WCAG 2.2 SC 4.1.3),
          // rendered from the start so assistive technology announces the text when it appears;
          // while empty it adds no Stack gap.
          <div role="status">{statusText}</div>
        ) : statusText === null ? null : (
          // Inside one, the provider's region announces the outcome and outlives this card, so
          // this line stays visible without being a second live region.
          <div>{statusText}</div>
        )}
        {decisionDialog}
      </Stack>
    </div>
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
   * before the evidence table: for example a budget card's utilization beside its efficiency.
   * With `reasonPlacement="fields"` the card prints no reason sentence and the fields carry the
   * reason themselves, as a keyword card's term, spend, conversions, cost/conversion and reason
   * fields do.
   */
  children?: ReactNode;
  /**
   * Where the item's reason sentence is printed:
   *
   * - `'card'` (the default): first in the body, before `children` (AAP 0.5.5 card body), as
   *   the Campaigns, Budget and Copy/creative cards show it;
   * - `'fields'`: within the section's own fields (`children`), which list the reason in their
   *   fixed order, so the card prints no reason line and the reason appears once. Keyword cards
   *   use it, because their five fields end with the reason (AAP 0.1.2: term, spend,
   *   conversions, cost/conversion, reason).
   */
  reasonPlacement?: 'card' | 'fields';
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
 *   <FieldList fields={budgetFieldList(item)} label="Budget details" />
 * </RecommendationCard>
 *
 * <RecommendationCard item={keywordItem} reasonPlacement="fields">
 *   <FieldList fields={keywordFieldList(keywordItem)} label="Keyword details" />
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
  reasonPlacement,
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
  // The card's own reason line; none when the section's fields list the reason themselves.
  const reason = reasonPlacement === 'fields' ? null : textOrNull(item.reason);
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
        figure={headlineFigure(item)}
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
              {/* A link to each discrepancy that holds the item (AAP 0.5.5). */}
              <BlockingDiscrepancyLinks item={item} />
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
