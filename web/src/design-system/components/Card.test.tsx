/**
 * Contract tests for `Card` and its `Card.Header`, `Card.Body` and `Card.Footer` slots
 * (AAP 0.4.2; `../GAPS.md`, binding procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: the card is an `<article>` named by the
 * heading `Card.Header` renders (`<h3>` by default, `<h4>` on request); the rank reads as
 * "Rank {rank}" through a visually hidden prefix; the rank and entity line are skipped for values
 * React renders as nothing (`undefined`, `null`, booleans, `''`) but kept for `0`; a header outside
 * a card still gets an id of its own; and the body and footer hold their children.
 */
import { render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { Card } from './Card';

/** Slot values React renders as nothing, each of which must leave no container behind. */
const EMPTY_SLOTS: readonly [string, ReactNode][] = [
  ['undefined', undefined],
  ['null', null],
  ['false', false],
  ['true', true],
  ['the empty string', ''],
];

describe('Card', () => {
  it('is an article named by its header heading, an h3 by default', () => {
    render(
      <Card id="rec-42">
        <Card.Header title="Pause" />
        <Card.Body>
          <p>Spent on 26 of 28 days with 0 leads.</p>
        </Card.Body>
      </Card>,
    );

    const article = screen.getByRole('article', { name: 'Pause' });
    expect(article.id).toBe('rec-42');
    expect(article.className).toBe('ds-card');

    const heading = within(article).getByRole('heading', { level: 3, name: 'Pause' });
    expect(heading.className).toBe('ds-card__title');
    expect(heading.id).not.toBe('');
    expect(article.getAttribute('aria-labelledby')).toBe(heading.id);
  });

  it('renders an h4 headline when headingLevel is 4', () => {
    render(
      <Card>
        <Card.Header title="Scale" headingLevel={4} />
        <Card.Body>Body</Card.Body>
      </Card>,
    );

    const article = screen.getByRole('article', { name: 'Scale' });
    expect(within(article).getByRole('heading', { level: 4, name: 'Scale' })).toBeDefined();
    expect(within(article).queryByRole('heading', { level: 3 })).toBeNull();
  });

  it('labels each card by its own heading', () => {
    render(
      <>
        <Card>
          <Card.Header title="Pause" />
          <Card.Body>First</Card.Body>
        </Card>
        <Card>
          <Card.Header title="Launch" />
          <Card.Body>Second</Card.Body>
        </Card>
      </>,
    );

    const pause = screen.getByRole('article', { name: 'Pause' });
    const launch = screen.getByRole('article', { name: 'Launch' });
    expect(pause.getAttribute('aria-labelledby')).not.toBe(
      launch.getAttribute('aria-labelledby'),
    );
  });

  it('reads the rank as "Rank {rank}" through a visually hidden prefix, before the headline', () => {
    const { container } = render(
      <Card>
        <Card.Header rank={1} title="Pause" meta="Spring webinar · LinkedIn" />
        <Card.Body>Body</Card.Body>
      </Card>,
    );

    const rank = container.querySelector('.ds-card__rank');
    expect(rank?.textContent).toBe('Rank 1');
    expect(rank?.querySelector('.ds-visually-hidden')?.textContent).toBe('Rank ');
    expect(rank?.nextElementSibling?.tagName).toBe('H3');

    const meta = container.querySelector('.ds-card__meta');
    expect(meta?.textContent).toBe('Spring webinar · LinkedIn');
    expect(meta?.previousElementSibling?.tagName).toBe('H3');
    // Neither the rank nor the entity line is part of the card's name.
    expect(screen.getByRole('article', { name: 'Pause' })).toBeDefined();
  });

  it.each(EMPTY_SLOTS)('renders no rank or entity line for %s', (_name, slot) => {
    const { container } = render(
      <Card>
        <Card.Header rank={slot} meta={slot} title="Hold" />
        <Card.Body>Body</Card.Body>
      </Card>,
    );

    const header = container.querySelector('.ds-card__header');
    expect(header?.children).toHaveLength(1);
    expect(container.querySelector('.ds-card__rank')).toBeNull();
    expect(container.querySelector('.ds-card__meta')).toBeNull();
    expect(screen.queryByText(/Rank/)).toBeNull();
  });

  it('renders a rank and an entity line of 0, because 0 is content', () => {
    const { container } = render(
      <Card>
        <Card.Header rank={0} meta={0} title="Review" />
        <Card.Body>Body</Card.Body>
      </Card>,
    );

    expect(container.querySelector('.ds-card__rank')?.textContent).toBe('Rank 0');
    expect(container.querySelector('.ds-card__meta')?.textContent).toBe('0');
  });

  it('gives a header rendered outside a card a heading id of its own', () => {
    render(<Card.Header title="Standalone" />);

    const heading = screen.getByRole('heading', { level: 3, name: 'Standalone' });
    expect(heading.id).not.toBe('');
  });

  it('renders the body and footer slots with their children, in order', () => {
    const { container } = render(
      <Card>
        <Card.Header title="Pause" />
        <Card.Body>
          <p>Reason</p>
        </Card.Body>
        <Card.Footer>
          <button type="button">Record decision</button>
          <button type="button">Dismiss</button>
        </Card.Footer>
      </Card>,
    );

    const article = screen.getByRole('article', { name: 'Pause' });
    expect(Array.from(article.children).map((child) => child.className)).toEqual([
      'ds-card__header',
      'ds-card__body',
      'ds-card__footer',
    ]);
    expect(container.querySelector('.ds-card__body')?.textContent).toBe('Reason');

    const footer = container.querySelector('.ds-card__footer');
    if (!(footer instanceof HTMLElement)) {
      throw new Error('The footer slot was not rendered');
    }
    expect(
      within(footer)
        .getAllByRole('button')
        .map((button) => button.textContent),
    ).toEqual(['Record decision', 'Dismiss']);
  });
});
