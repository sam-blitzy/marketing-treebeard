/**
 * `Card`: the product's recommendation card, with `Card.Header`, `Card.Body` and `Card.Footer`
 * slots (AAP 0.4.2, status GAP).
 *
 * Every action on `/actions` renders as one card (AAP 0.5.5):
 *
 * - `Card.Header`: the rank, the action as the headline, the headline number that justifies it
 *   (AAP 0.1.2: every card leads with one headline number), and the entity name with its
 *   source;
 * - `Card.Body`: the reason sentence, the evidence table, the window chip and the flags row;
 * - `Card.Footer`: the actions (Record decision, Mark done, Dismiss, Snooze).
 *
 * Feature code never renders raw heading elements (AAP 0.4.4), so `Card.Header` owns the
 * card's heading. The card is an `<article>` labelled by that heading, so assistive technology
 * announces each card by its action and the heading outline stays intact (WCAG 2.2 SC 1.3.1
 * Info and Relationships). The headline number sits outside the heading, so it never joins the
 * card's name. The styling lives in `Card.css`, whose class names are the contract this
 * component renders: `ds-card`, `ds-card__header`, `ds-card__rank`, `ds-card__title`,
 * `ds-card__figure`, `ds-card__figure-label`, `ds-card__figure-value`, `ds-card__meta`,
 * `ds-card__body` and `ds-card__footer`. The visually hidden "Rank " prefix uses
 * `ds-visually-hidden` from `shared.css`.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled by semantic tokens, except for the
 * headline's `flex: 1 1 auto` factor and the rank prefix's visually hidden `1px` box and
 * `inset(50%)` clip that `../GAPS.md` lists, and binding Blitzy OS later changes only this file
 * and `Card.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './shared.css';
import './Card.css';

import { createContext, useContext, useId } from 'react';
import type { ReactNode } from 'react';

/**
 * Props of {@link Card}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because
 * feature code must not restyle a wrapper (AAP 0.4.4). Content goes only into the slots.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS card or container component.
 */
export interface CardProps {
  /** The card's `id`, for example the target of a link to a blocking discrepancy. */
  id?: string;
  /**
   * The slots, in order: exactly one `Card.Header` (which supplies the card's accessible
   * name), then `Card.Body` and an optional `Card.Footer`.
   */
  children: ReactNode;
}

/**
 * Props of {@link Card.Header}.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS card header slot.
 */
export interface CardHeaderProps {
  /** The headline: the action the card recommends, for example "Pause". It names the card. */
  title: ReactNode;
  /**
   * The heading level of the headline. Defaults to `3`, below a section's `<h2>`; use `4` when
   * the card sits inside a group headed by an `<h3>`, so no heading level is skipped.
   */
  headingLevel?: 3 | 4;
  /**
   * The card's rank within its group, shown ahead of the headline. Assistive technology reads
   * it as "Rank {rank}". Omitted (or `null`, a boolean or an empty string), no rank is rendered,
   * so no "Rank" prefix is announced without a value; any other value, including `0`, renders
   * and reads as "Rank 0".
   */
  rank?: ReactNode;
  /**
   * The headline number that justifies the action, for example `{ label: 'Wasted spend', value:
   * '$186.40' }`. It is shown prominently after the headline and before the entity line, as a
   * paragraph that reads "{label} {value}", outside the heading, so the card's accessible name
   * stays the action. The caller passes the value already formatted; the header never computes
   * it. Omitted, or with a `label` or `value` that renders nothing (`undefined`, `null`, a
   * boolean or an empty string), no figure is rendered; any other value, including `0`, renders.
   */
  figure?: { label: ReactNode; value: ReactNode };
  /**
   * The entity line: the entity name and its source. Omitted (or `null`, a boolean or an empty
   * string), no entity line is rendered; any other value, including `0`, renders it.
   */
  meta?: ReactNode;
}

/**
 * Props of {@link Card.Body}.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS card content slot.
 */
export interface CardBodyProps {
  /** The reason sentence, the evidence table, the window chip and the flags row. */
  children: ReactNode;
}

/**
 * Props of {@link Card.Footer}.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS card actions slot.
 */
export interface CardFooterProps {
  /** The card's actions, such as Record decision, Mark done, Dismiss and Snooze buttons. */
  children: ReactNode;
}

/**
 * Carries the id of the card's heading from `Card` to `Card.Header`, so the `<article>` is
 * labelled by its headline through `aria-labelledby`. `null` outside a `Card`.
 */
const CardTitleIdContext = createContext<string | null>(null);

/**
 * Whether a slot value renders anything.
 *
 * React renders nothing for `undefined`, `null`, booleans and the empty string, so a
 * conditional slot such as `rank={ranked && position}` must not leave an empty container
 * behind: `Card.css` spaces the header parts with a flex gap, so an empty container would still
 * indent the headline, and an empty rank would still announce its visually hidden "Rank "
 * prefix with no value. Any other value, including `0`, renders and counts.
 *
 * @param node - the `rank` or `meta` value, or the `figure`'s `label` or `value`
 * @returns `true` when the slot has content to render
 */
function hasContent(node: ReactNode): boolean {
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * The card surface: an `<article>` labelled by the heading its `Card.Header` renders.
 *
 * Every card must contain exactly one `Card.Header`: it renders the heading whose id the
 * article's `aria-labelledby` references, so a card without one has no accessible name.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function CardRoot({ id, children }: CardProps) {
  const titleId = useId();

  return (
    <CardTitleIdContext value={titleId}>
      <article id={id} className="ds-card" aria-labelledby={titleId}>
        {children}
      </article>
    </CardTitleIdContext>
  );
}

/**
 * The header slot: the optional rank, the headline as an `<h3>` or `<h4>`, the optional
 * headline figure and the optional entity line. Inside a `Card`, the heading takes the id the
 * card's `aria-labelledby` points at; rendered on its own, it falls back to an id of its own, so
 * the heading is never left without one. The figure is a `<p>` beside the heading, never inside
 * it, so it is read after the action and never becomes part of the card's name.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function CardHeader({ title, headingLevel = 3, rank, figure, meta }: CardHeaderProps) {
  // Both hooks run on every render, whether or not a Card provides the id (Rules of Hooks).
  const contextId = useContext(CardTitleIdContext);
  const ownId = useId();
  const headingId = contextId ?? ownId;
  const Heading = headingLevel === 4 ? 'h4' : 'h3';
  const showFigure =
    figure !== undefined && hasContent(figure.label) && hasContent(figure.value);

  return (
    <div className="ds-card__header">
      {hasContent(rank) && (
        <span className="ds-card__rank">
          <span className="ds-visually-hidden">Rank </span>
          {rank}
        </span>
      )}
      <Heading id={headingId} className="ds-card__title">
        {title}
      </Heading>
      {showFigure && (
        <p className="ds-card__figure">
          <span className="ds-card__figure-label">{figure.label}</span>{' '}
          <span className="ds-card__figure-value">{figure.value}</span>
        </p>
      )}
      {hasContent(meta) && <div className="ds-card__meta">{meta}</div>}
    </div>
  );
}

/**
 * The body slot: a column holding the reason, the evidence, the window chip and the flags.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function CardBody({ children }: CardBodyProps) {
  return <div className="ds-card__body">{children}</div>;
}

/**
 * The footer slot: the card's actions, set off from the body by a divider and wrapping onto
 * further lines on narrow screens.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function CardFooter({ children }: CardFooterProps) {
  return <div className="ds-card__footer">{children}</div>;
}

/**
 * Recommendation card with three slots:
 *
 * - `Card.Header` ({@link CardHeaderProps}): rank, headline, headline figure and entity line;
 *   required, because its heading is the card's accessible name;
 * - `Card.Body` ({@link CardBodyProps}): reason, evidence, window chip and flags;
 * - `Card.Footer` ({@link CardFooterProps}): the actions.
 *
 * Renders `<article class="ds-card" aria-labelledby="{heading id}">`. The components hold no
 * state and have no effects; their output depends only on their props.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <Card id="rec-42">
 *   <Card.Header
 *     rank={1}
 *     title="Pause"
 *     figure={{ label: 'Spend', value: '$1,240.00' }}
 *     meta="Spring webinar · LinkedIn"
 *     headingLevel={4}
 *   />
 *   <Card.Body>
 *     <p>Spent on 26 of 28 days with 0 leads against 4.1 expected.</p>
 *   </Card.Body>
 *   <Card.Footer>
 *     <Button variant="primary">Record decision</Button>
 *     <Button variant="quiet">Dismiss</Button>
 *   </Card.Footer>
 * </Card>
 * ```
 */
export const Card = Object.assign(CardRoot, {
  /**
   * Header slot: rank, headline (the card's name), headline figure and entity line.
   * GAP: bind to Blitzy OS on source arrival.
   */
  Header: CardHeader,
  /**
   * Body slot: reason, evidence, window chip and flags.
   * GAP: bind to Blitzy OS on source arrival.
   */
  Body: CardBody,
  /** Footer slot: the card's actions. GAP: bind to Blitzy OS on source arrival. */
  Footer: CardFooter,
});
