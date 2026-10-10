/**
 * Component tests for the bounded batches of `/actions` (`batches.tsx`; AAP 0.1.1 Performance,
 * AAP 0.5.5 Behaviour).
 *
 * They hold the module to its TSDoc contract:
 * - a list at or under one batch renders exactly as an unbatched list: every entry, no status,
 *   no button, no extra element;
 * - a longer list renders its first batch in the given order, with a polite status "Showing
 *   {shown} of {total} {noun}" and a "Show {next} more {noun}" button; each press reveals the
 *   next batch until every entry is shown, the status reads "Showing all {total} {noun}" and the
 *   button is gone;
 * - after a reveal, focus moves to the first newly revealed entry (made focusable with
 *   `tabindex="-1"` only while it holds focus), or to the status when that entry has no target;
 * - a hash naming an entry beyond the shown batches reveals through it and scrolls it into view,
 *   on mount and on `hashchange`, and on `hashchange` alone also focuses it (or the entry's own
 *   target), without scrolling again; a hash naming a shown or unknown entry changes nothing;
 * - table rows are sliced, the control follows the table and focus lands on the row header of
 *   the first new row;
 * - the status is never an `alert`.
 *
 * The module fetches nothing, so every test renders without providers or a network stub.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Button, DataTable } from '../../design-system';
import type { DataTableColumn } from '../../design-system';
import { LARGE_RENDER_TEST_TIMEOUT_MS } from '../../test/timeouts';
import {
  ACTION_NOUN,
  BatchControl,
  BatchEntryAnchor,
  BatchedList,
  CARD_BATCH_SIZE,
  GROUP_BATCH_SIZE,
  GROUP_NOUN,
  ROW_BATCH_SIZE,
  batchStatusText,
  showMoreLabel,
  useBatches,
} from './batches';
import type { BatchNoun } from './batches';

// ---------------------------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------------------------

/** One entry of a test list: an id and the rank the API gave it. */
interface Entry {
  readonly id: number;
  readonly rank: number;
}

/** `count` entries with ids `1..count`, ranked in that order. */
function entries(count: number): Entry[] {
  return Array.from({ length: count }, (_, index) => ({ id: index + 1, rank: index + 1 }));
}

/** The anchor id of an entry's card, as `RecommendationCard` renders it. */
function anchorOf(entry: Entry): string {
  return `item-${String(entry.id)}`;
}

/** Props of {@link CardList}. */
interface CardListProps {
  readonly items: readonly Entry[];
  readonly batchSize?: number;
  readonly hashAware?: boolean;
  readonly focusIdOf?: (entry: Entry) => string;
}

/** A batched list of article "cards", each anchored at `item-{id}` and showing its rank. */
function CardList({
  items,
  batchSize = CARD_BATCH_SIZE,
  hashAware = true,
  focusIdOf = anchorOf,
}: CardListProps) {
  const card = (entry: Entry) => (
    <article id={anchorOf(entry)} aria-label={`Card ${String(entry.id)}`} data-rank={entry.rank}>
      {`Rank ${String(entry.rank)}`}
    </article>
  );
  return (
    <div data-testid="list">
      {hashAware ? (
        <BatchedList
          entries={items}
          batchSize={batchSize}
          noun={ACTION_NOUN}
          getKey={(entry) => entry.id}
          renderEntry={card}
          anchorsOf={(entry) => [anchorOf(entry)]}
          focusIdOf={focusIdOf}
        />
      ) : (
        <BatchedList
          entries={items}
          batchSize={batchSize}
          noun={ACTION_NOUN}
          getKey={(entry) => entry.id}
          renderEntry={card}
          focusIdOf={focusIdOf}
        />
      )}
    </div>
  );
}

/** The ranks of every rendered card, in document order. */
function renderedRanks(): number[] {
  return screen.getAllByRole('article').map((article) => Number(article.dataset.rank));
}

/** The ranks `1..count`. */
function ranksThrough(count: number): number[] {
  return Array.from({ length: count }, (_, index) => index + 1);
}

/** The "Show … more" button, or `null` when the list shows every entry. */
function showMore(): HTMLElement | null {
  return screen.queryByRole('button', { name: /^Show \d+ more/u });
}

/** Points `location.hash` at `id` without letting jsdom fire its own `hashchange`. */
function setHash(id: string): void {
  window.history.replaceState(null, '', `#${id}`);
}

/** Clears `location.hash`. */
function clearHash(): void {
  window.history.replaceState(null, '', window.location.pathname);
}

/** A stand-in for `Element.prototype.scrollIntoView`, which jsdom does not implement. */
let scrollIntoView: ReturnType<typeof vi.fn>;

beforeEach(() => {
  scrollIntoView = vi.fn();
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    configurable: true,
    writable: true,
    value: scrollIntoView,
  });
});

afterEach(() => {
  clearHash();
  Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
});

// ---------------------------------------------------------------------------------------------
// Batch sizes and labels
// ---------------------------------------------------------------------------------------------

describe('batch sizes and labels', () => {
  it('bounds cards, groups and table rows by named sizes', () => {
    expect(CARD_BATCH_SIZE).toBe(20);
    expect(GROUP_BATCH_SIZE).toBe(10);
    expect(ROW_BATCH_SIZE).toBe(50);
  });

  it('names the next batch and the count in the singular or the plural', () => {
    expect(showMoreLabel(20, ACTION_NOUN)).toBe('Show 20 more actions');
    expect(showMoreLabel(1, GROUP_NOUN)).toBe('Show 1 more group');
    expect(batchStatusText(20, 1340, ACTION_NOUN)).toBe('Showing 20 of 1,340 actions');
    expect(batchStatusText(340, 340, ACTION_NOUN)).toBe('Showing all 340 actions');
    expect(batchStatusText(1, 1, GROUP_NOUN)).toBe('Showing all 1 group');
  });
});

// ---------------------------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------------------------

describe('BatchedList', () => {
  it('renders a list at the batch size exactly as unbatched: every entry and no control', () => {
    render(<CardList items={entries(CARD_BATCH_SIZE)} />);

    expect(renderedRanks()).toEqual(ranksThrough(CARD_BATCH_SIZE));
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    // The cards are the list's only elements: no batch container wraps them.
    const list = screen.getByTestId('list');
    expect([...list.children].every((child) => child.tagName === 'ARTICLE')).toBe(true);
  });

  it('renders the first batch of a long list in API order, with its status and button', () => {
    render(<CardList items={entries(250)} />);

    expect(renderedRanks()).toEqual(ranksThrough(20));
    expect(screen.getByRole('status').textContent).toBe('Showing 20 of 250 actions');
    expect(showMore()?.textContent).toBe('Show 20 more actions');
    // The cards stay direct children of the enclosing element, followed by the control.
    const list = screen.getByTestId('list');
    expect([...list.children].slice(0, 20).every((child) => child.tagName === 'ARTICLE')).toBe(
      true,
    );
    expect(list.children).toHaveLength(21);
  });

  it('reveals batch after batch to the end, keeping the order, then removes the button', () => {
    render(<CardList items={entries(250)} />);

    for (let shown = 40; shown <= 240; shown += 20) {
      const button = showMore();
      expect(button?.textContent).toBe('Show 20 more actions');
      fireEvent.click(button as HTMLElement);
      expect(renderedRanks()).toEqual(ranksThrough(shown));
      expect(screen.getByRole('status').textContent).toBe(
        `Showing ${String(shown)} of 250 actions`,
      );
    }
    expect(showMore()?.textContent).toBe('Show 10 more actions');
    fireEvent.click(showMore() as HTMLElement);

    expect(renderedRanks()).toEqual(ranksThrough(250));
    expect(showMore()).toBeNull();
    expect(screen.getByRole('status').textContent).toBe('Showing all 250 actions');
    expect(screen.queryByRole('alert')).toBeNull();
  }, LARGE_RENDER_TEST_TIMEOUT_MS);

  it('moves focus to the first revealed card, focusable only while it holds focus', () => {
    render(<CardList items={entries(45)} />);

    fireEvent.click(showMore() as HTMLElement);
    const first = screen.getByRole('article', { name: 'Card 21' });
    expect(document.activeElement).toBe(first);
    expect(first.getAttribute('tabindex')).toBe('-1');

    // The last reveal removes the button; focus still lands on the first new card.
    fireEvent.click(showMore() as HTMLElement);
    const last = screen.getByRole('article', { name: 'Card 41' });
    expect(document.activeElement).toBe(last);
    expect(first.hasAttribute('tabindex')).toBe(false);
    expect(showMore()).toBeNull();

    last.blur();
    expect(last.hasAttribute('tabindex')).toBe(false);
  });

  it('moves focus to the status when the first revealed entry has no focus target', () => {
    render(<CardList items={entries(30)} focusIdOf={() => 'no-such-element'} />);

    fireEvent.click(showMore() as HTMLElement);

    const status = screen.getByRole('status');
    expect(document.activeElement).toBe(status);
    expect(status.getAttribute('tabindex')).toBe('-1');
    expect(status.textContent).toBe('Showing all 30 actions');
  });

  it('reveals through the entry the hash names on mount, and scrolls it into view', () => {
    setHash('item-75');
    render(<CardList items={entries(250)} />);

    expect(renderedRanks()).toEqual(ranksThrough(80));
    expect(screen.getByRole('status').textContent).toBe('Showing 80 of 250 actions');
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(screen.getByRole('article', { name: 'Card 75' }));
    // A hash present on mount, as on a page load, moves no focus.
    expect(document.activeElement).toBe(document.body);
    expect(screen.getByRole('article', { name: 'Card 75' }).hasAttribute('tabindex')).toBe(false);
  });

  it('reveals through the entry a hash change names, scrolls it into view and focuses it', () => {
    render(<CardList items={entries(250)} />);
    expect(renderedRanks()).toHaveLength(20);
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');

    act(() => {
      setHash('item-130');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    expect(renderedRanks()).toEqual(ranksThrough(140));
    const target = screen.getByRole('article', { name: 'Card 130' });
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(target);
    // Focus lands on the revealed card, focusable only while it holds focus, without scrolling
    // it again.
    expect(document.activeElement).toBe(target);
    expect(target.getAttribute('tabindex')).toBe('-1');
    expect(focus.mock.contexts).toEqual([target]);
    expect(focus.mock.calls).toEqual([[{ preventScroll: true }]]);
    focus.mockRestore();
    target.blur();
    expect(target.hasAttribute('tabindex')).toBe(false);
    // The button keeps working from the new count.
    fireEvent.click(showMore() as HTMLElement);
    expect(renderedRanks()).toHaveLength(160);
    expect(document.activeElement).toBe(screen.getByRole('article', { name: 'Card 141' }));
  });

  it('ignores a hash naming a shown or an unknown entry, and a list that is not hash-aware', () => {
    setHash('item-5');
    const { unmount } = render(<CardList items={entries(100)} />);
    expect(renderedRanks()).toHaveLength(20);

    act(() => {
      setHash('elsewhere');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(renderedRanks()).toHaveLength(20);
    unmount();

    setHash('item-90');
    render(<CardList items={entries(100)} hashAware={false} />);
    act(() => {
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(renderedRanks()).toHaveLength(20);
    expect(scrollIntoView).not.toHaveBeenCalled();
    // Nothing was revealed, so focus stays where it was.
    expect(document.activeElement).toBe(document.body);
  });

  it('decodes a percent-encoded hash, keeps a malformed one as written, and needs no scrolling', () => {
    // Without layout (no scrollIntoView), revealing still works and nothing throws.
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
    setHash('item-%37%35');
    const { unmount } = render(<CardList items={entries(100)} />);
    expect(renderedRanks()).toEqual(ranksThrough(80));
    unmount();

    setHash('item-%E0%A4%A');
    render(<CardList items={entries(100)} />);
    expect(renderedRanks()).toEqual(ranksThrough(20));
  });

  it("scrolls to the entry's own target when the hash names an element it does not render", () => {
    // Each entry also holds an anchor nothing renders, as a group holds a collapsed held card's.
    function Unrendered() {
      return (
        <BatchedList
          entries={entries(150)}
          batchSize={CARD_BATCH_SIZE}
          noun={ACTION_NOUN}
          getKey={(entry) => entry.id}
          renderEntry={(entry) => (
            <article id={anchorOf(entry)} aria-label={`Card ${String(entry.id)}`}>
              {String(entry.id)}
            </article>
          )}
          anchorsOf={(entry) => [anchorOf(entry), `held-${String(entry.id)}`]}
          focusIdOf={anchorOf}
        />
      );
    }
    setHash('held-75');
    render(<Unrendered />);
    expect(scrollIntoView.mock.contexts).toEqual([
      screen.getByRole('article', { name: 'Card 75' }),
    ]);

    expect(document.activeElement).toBe(document.body);

    act(() => {
      setHash('held-130');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    const own = screen.getByRole('article', { name: 'Card 130' });
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(own);
    // The entry's own focus target takes the focus the unrendered element cannot.
    expect(document.activeElement).toBe(own);
    expect(own.getAttribute('tabindex')).toBe('-1');
  });

  it('reveals but moves no focus when neither the named element nor its entry target renders', () => {
    render(
      <>
        <Button>Elsewhere</Button>
        <BatchedList
          entries={entries(100)}
          batchSize={CARD_BATCH_SIZE}
          noun={ACTION_NOUN}
          getKey={(entry) => entry.id}
          renderEntry={(entry) => (
            <article id={anchorOf(entry)} aria-label={`Card ${String(entry.id)}`}>
              {String(entry.id)}
            </article>
          )}
          anchorsOf={(entry) => [anchorOf(entry), `held-${String(entry.id)}`]}
          focusIdOf={() => 'no-such-element'}
        />
      </>,
    );
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' });
    elsewhere.focus();

    act(() => {
      setHash('held-90');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    expect(screen.getAllByRole('article')).toHaveLength(100);
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(elsewhere);
  });

  it('ignores a reveal once every entry is shown', () => {
    function Revealer() {
      const batches = useBatches(entries(30), { batchSize: CARD_BATCH_SIZE });
      return (
        <>
          <span data-testid="shown">{batches.shown.length}</span>
          <Button onClick={batches.revealNext}>Reveal</Button>
        </>
      );
    }
    render(<Revealer />);
    const reveal = screen.getByRole('button', { name: 'Reveal' });

    fireEvent.click(reveal);
    expect(screen.getByTestId('shown').textContent).toBe('30');
    fireEvent.click(reveal);
    expect(screen.getByTestId('shown').textContent).toBe('30');
  });

  it('keeps the shown count when the entries change, and reads a bad batch size as 1', () => {
    function Changing() {
      const [count, setCount] = useState(60);
      return (
        <>
          <Button
            onClick={() => {
              setCount((previous) => previous - 30);
            }}
          >
            Shrink
          </Button>
          <CardList items={entries(count)} />
        </>
      );
    }
    const { unmount } = render(<Changing />);
    fireEvent.click(showMore() as HTMLElement);
    expect(renderedRanks()).toHaveLength(40);
    fireEvent.click(screen.getByRole('button', { name: 'Shrink' }));
    expect(renderedRanks()).toEqual(ranksThrough(30));
    expect(screen.getByRole('status').textContent).toBe('Showing all 30 actions');
    unmount();

    render(<CardList items={entries(3)} batchSize={0} />);
    expect(renderedRanks()).toEqual([1]);
    expect(showMore()?.textContent).toBe('Show 1 more action');
  });
});

// ---------------------------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------------------------

/** One row of the test table, with the focus target of its row header. */
interface Row {
  readonly entry: Entry;
  readonly focusId: string | null;
}

/** What the test table counts. */
const ROW_NOUN: BatchNoun = { singular: 'row', plural: 'rows' };

/** The test table's columns: the row header carries the focus target. */
const COLUMNS: readonly DataTableColumn<Row>[] = [
  {
    key: 'name',
    header: 'Name',
    isRowHeader: true,
    render: (row) => (
      <BatchEntryAnchor id={row.focusId}>{`Row ${String(row.entry.id)}`}</BatchEntryAnchor>
    ),
  },
  { key: 'rank', header: 'Rank', align: 'end', render: (row) => String(row.entry.rank) },
];

/** A table of `count` rows, sliced by {@link useBatches}, with its control after it. */
function BatchedTable({ count }: { count: number }) {
  const batches = useBatches(entries(count), { batchSize: ROW_BATCH_SIZE });
  const rows = batches.shown.map((entry, index) => ({
    entry,
    focusId: batches.batched ? batches.entryId(index) : null,
  }));
  return (
    <>
      <DataTable
        caption={`Rows (${String(count)})`}
        columns={COLUMNS}
        rows={rows}
        getRowKey={(row) => String(row.entry.id)}
      />
      <BatchControl batches={batches} noun={ROW_NOUN} />
    </>
  );
}

describe('batched tables', () => {
  it('slices the rows, keeps the caption and puts the control after the table', () => {
    render(<BatchedTable count={120} />);

    const table = screen.getByRole('table', { name: 'Rows (120)' });
    expect(within(table).getAllByRole('row')).toHaveLength(51);
    expect(within(table).getAllByRole('rowheader')[0]?.textContent).toBe('Row 1');
    const status = screen.getByRole('status');
    expect(status.textContent).toBe('Showing 50 of 120 rows');
    expect((table.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(
      true,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show 50 more rows' }));
    const headers = within(table).getAllByRole('rowheader');
    expect(headers).toHaveLength(100);
    expect(document.activeElement?.textContent).toBe('Row 51');
    expect(headers[50]?.contains(document.activeElement)).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Show 20 more rows' }));
    expect(within(table).getAllByRole('rowheader')).toHaveLength(120);
    expect(document.activeElement?.textContent).toBe('Row 101');
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders a table at the batch size with every row, no anchor and no control', () => {
    render(<BatchedTable count={ROW_BATCH_SIZE} />);

    const table = screen.getByRole('table', { name: 'Rows (50)' });
    expect(within(table).getAllByRole('rowheader')).toHaveLength(50);
    expect(table.querySelector('span[id]')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });
});
