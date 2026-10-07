/**
 * `Card`: the product's recommendation card, with `Card.Header`, `Card.Body` and `Card.Footer`
 * slots (AAP 0.4.2, status GAP).
 *
 * Every action on `/actions` renders as one card (AAP 0.5.5):
 *
 * - `Card.Header`: the rank, the action as the headline, and the entity name with its source;
 * - `Card.Body`: the reason sentence, the evidence table, the window chip and the flags row;
 * - `Card.Footer`: the actions (Record decision, Mark done, Dismiss, Snooze).
 *
 * Feature code never renders raw heading elements (AAP 0.4.4), so `Card.Header` owns the
 * card's heading. The card is an `<article>` labelled by that heading, so assistive technology
 * announces each card by its action and the heading outline stays intact (WCAG 2.2 SC 1.3.1
 * Info and Relationships). The styling lives in `Card.css`, whose class names are the contract
 * this component renders: `ds-card`, `ds-card__header`, `ds-card__rank`, `ds-card__title`,
 * `ds-card__meta`, `ds-card__body` and `ds-card__footer`. The visually hidden "Rank " prefix
 * uses `ds-visually-hidden` from `shared.css`.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `Card.css` (AAP 0.4.5, `../GAPS.md`).
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
   * it as "Rank {rank}". Omitted, no rank is rendered.
   */
  rank?: ReactNode;
  /** The entity line: the entity name and its source. Omitted, no entity line is rendered. */
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
 * The header slot: the optional rank, the headline as an `<h3>` or `<h4>`, and the optional
 * entity line. Inside a `Card`, the heading takes the id the card's `aria-labelledby` points
 * at; rendered on its own, it falls back to an id of its own, so the heading is never left
 * without one.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function CardHeader({ title, headingLevel = 3, rank, meta }: CardHeaderProps) {
  // Both hooks run on every render, whether or not a Card provides the id (Rules of Hooks).
  const contextId = useContext(CardTitleIdContext);
  const ownId = useId();
  const headingId = contextId ?? ownId;
  const Heading = headingLevel === 4 ? 'h4' : 'h3';

  return (
    <div className="ds-card__header">
      {rank !== undefined && (
        <span className="ds-card__rank">
          <span className="ds-visually-hidden">Rank </span>
          {rank}
        </span>
      )}
      <Heading id={headingId} className="ds-card__title">
        {title}
      </Heading>
      {meta !== undefined && <div className="ds-card__meta">{meta}</div>}
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
 * - `Card.Header` ({@link CardHeaderProps}): rank, headline and entity line; required, because
 *   its heading is the card's accessible name;
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
 *   <Card.Header rank={1} title="Pause" meta="Spring webinar · LinkedIn" headingLevel={4} />
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
   * Header slot: rank, headline (the card's name) and entity line.
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
