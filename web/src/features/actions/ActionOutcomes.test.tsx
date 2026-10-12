/**
 * Tests for `ActionOutcomes`, the card outcomes of `/actions` that outlive the card (WCAG 2.2
 * SC 4.1.3), held to its TSDoc contract:
 * - outside a provider every hook reads nothing: no announcer, no recorder and the shared empty
 *   map of confirmations;
 * - the announcement: the provider's pre-mounted polite region shows each message trimmed and
 *   ignores a blank one, and is never an alert;
 * - the outcome's run view and recommendation: a change of `scope` empties the same region and
 *   clears every confirmation, and an announcer captured before it announces nothing after it;
 *   an outcome or confirmation whose recommendation leaves `identities` is dropped, one still
 *   listed stays, a confirmation for an unlisted recommendation is not recorded, and an
 *   outcome naming no recommendation, or a provider without `identities`, is not tied to the
 *   list;
 * - the provider's confirmation store: a confirmation is kept by recommendation identity, its
 *   message trimmed, a blank identity or message and an identical repeat change nothing, a
 *   later feedback on the same recommendation replaces it, and recording announces nothing;
 * - a reader re-renders only when a confirmation among its own identities changes, and keeps
 *   the same map between renders otherwise, so a feedback in one memoized section leaves the
 *   others as they are;
 * - `FeedbackConfirmation`: a success banner titled with the message in an unstyled
 *   `tabindex="-1"` focus target carrying the recommendation identity, never a live region, an
 *   alert or a Tab stop; it takes the focus of a card of its recommendation that leaves holding
 *   it, scrolled into view as far as needed (`block: 'nearest'`), never focus the user put
 *   elsewhere, keeps it under StrictMode's replayed effects, and hands focus it holds as it
 *   leaves to the card now in its place, on its `<article>` (or a row's first enabled action
 *   outside every card), revealed as far as needed, a card taller than the viewport with its top
 *   aligned (`block: 'start'`);
 * - a card records its feedback's confirmation, under its item's identity, only once the
 *   feedback is recorded: a refused feedback leaves none.
 *
 * The store tests render plain components; the card test stubs only `fetch`.
 */
import { apiError, json, mockApi } from '../../test/mockApi';

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Item, RunId } from '../../api/types';
import { Button } from '../../design-system';
import {
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeFeedback,
  makeRecommendationsPage,
} from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import {
  ActionOutcomeProvider,
  FeedbackConfirmation,
  NO_FEEDBACK_CONFIRMATIONS,
  useActionOutcome,
  useFeedbackConfirmations,
  useRecordFeedbackConfirmation,
} from './ActionOutcomes';
import type { AnnounceActionOutcome, FeedbackConfirmations } from './ActionOutcomes';
import { useFocusLeaving } from './carriedFocus';
import { FEEDBACK_LABELS, RecommendationCard } from './RecommendationCard';
import { itemIdentity } from './sections/SectionFrame';

// ---------------------------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------------------------

/** What the harness components saw, by name: their renders and the last values they read. */
interface Seen {
  renders: Map<string, number>;
  confirmations: Map<string, FeedbackConfirmations>;
}

/** A fresh record of what the harness saw. */
function newSeen(): Seen {
  return { renders: new Map(), confirmations: new Map() };
}

/** Props of {@link Reader}. */
interface ReaderProps {
  name: string;
  identities: readonly string[];
  seen: Seen;
}

/** Reads the confirmations of `identities`, counting its renders, and lists them as text. */
function Reader({ name, identities, seen }: ReaderProps): ReactNode {
  const confirmations = useFeedbackConfirmations(identities);
  seen.renders.set(name, (seen.renders.get(name) ?? 0) + 1);
  seen.confirmations.set(name, confirmations);
  return (
    <ul aria-label={name}>
      {[...confirmations].map(([identity, message]) => (
        <li key={identity}>{`${identity} → ${message}`}</li>
      ))}
    </ul>
  );
}

/** Props of {@link Recorder}. */
interface RecorderProps {
  /** The button's name, which a test presses through {@link record}. */
  label: string;
  identity: string;
  message: string;
}

/** Records `message` under `identity` in the nearest provider when its button is pressed. */
function Recorder({ label, identity, message }: RecorderProps): ReactNode {
  const recordConfirmation = useRecordFeedbackConfirmation();
  return (
    <Button
      onClick={() => {
        recordConfirmation?.(identity, message);
      }}
    >
      {label}
    </Button>
  );
}

/** Presses the recorder named `label`. */
function record(label: string): void {
  fireEvent.click(screen.getByRole('button', { name: label }));
}

/** The provider's polite region: the only `role="status"` element of these tests. */
function outcomeRegion(): HTMLElement {
  return screen.getByRole('status');
}

/** Two stable identity lists, as a memoized section passes them. */
const ALPHA = Object.freeze(['alpha']);
const BETA = Object.freeze(['beta']);

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('ActionOutcomes outside a provider', () => {
  it('reads no announcer, no recorder and the shared empty map', () => {
    const seen = newSeen();
    const read: { announce?: unknown; record?: unknown } = {};
    function Probe(): ReactNode {
      read.announce = useActionOutcome();
      read.record = useRecordFeedbackConfirmation();
      return null;
    }
    render(
      <>
        <Probe />
        <Reader name="Alpha" identities={ALPHA} seen={seen} />
      </>,
    );

    expect(read.announce).toBeNull();
    expect(read.record).toBeNull();
    expect(seen.confirmations.get('Alpha')).toBe(NO_FEEDBACK_CONFIRMATIONS);
    expect(NO_FEEDBACK_CONFIRMATIONS.size).toBe(0);
    expect(screen.queryByRole('status')).toBeNull();
  });
});

describe('ActionOutcomes: the confirmation store', () => {
  /**
   * Renders a provider with readers of `alpha` and `beta` and a recorder for each
   * `[label, identity, message]` given.
   */
  function renderStore(recorders: readonly [string, string, string][]): Seen {
    const seen = newSeen();
    render(
      <ActionOutcomeProvider>
        {recorders.map(([label, identity, message]) => (
          <Recorder key={label} label={label} identity={identity} message={message} />
        ))}
        <Reader name="Alpha" identities={ALPHA} seen={seen} />
        <Reader name="Beta" identities={BETA} seen={seen} />
      </ActionOutcomeProvider>,
    );
    return seen;
  }

  it('keeps a confirmation by identity, trimmed, and re-renders only the reader of that identity', () => {
    const seen = renderStore([['Dismiss', 'alpha', '  Dismissed: Pause (Generic CRM — US)  ']]);
    const empty = seen.confirmations.get('Alpha');
    expect(empty).toBe(NO_FEEDBACK_CONFIRMATIONS);
    const before = { alpha: seen.renders.get('Alpha'), beta: seen.renders.get('Beta') };

    record('Dismiss');

    expect(
      within(screen.getByRole('list', { name: 'Alpha' }))
        .getAllByRole('listitem')
        .map((line) => line.textContent),
    ).toEqual(['alpha → Dismissed: Pause (Generic CRM — US)']);
    expect(seen.renders.get('Alpha')).toBe((before.alpha ?? 0) + 1);
    // The reader of another recommendation is not rendered again.
    expect(seen.renders.get('Beta')).toBe(before.beta);
    expect(seen.confirmations.get('Beta')).toBe(NO_FEEDBACK_CONFIRMATIONS);
    // Recording announces nothing: the card announces through the provider's announcer.
    expect(outcomeRegion().textContent).toBe('');
  });

  it('ignores a blank identity or message and an identical repeat, and lets a later feedback replace it', () => {
    const seen = renderStore([
      ['Dismiss', 'alpha', 'Dismissed: Pause (Generic CRM — US)'],
      ['Dismiss again', 'alpha', 'Dismissed: Pause (Generic CRM — US) '],
      ['Snooze', 'alpha', 'Snoozed: Pause (Generic CRM — US)'],
      ['Blank message', 'alpha', '   '],
      ['Blank identity', '  ', 'Dismissed: Pause (Generic CRM — US)'],
    ]);
    record('Dismiss');
    const first = seen.confirmations.get('Alpha');
    const renders = seen.renders.get('Alpha');

    // The same message again, even with trailing space, changes nothing and renders nothing.
    record('Dismiss');
    record('Dismiss again');
    expect(seen.renders.get('Alpha')).toBe(renders);
    expect(seen.confirmations.get('Alpha')).toBe(first);

    // Blank input is ignored.
    record('Blank message');
    record('Blank identity');
    expect(seen.renders.get('Alpha')).toBe(renders);
    expect(first?.get('alpha')).toBe('Dismissed: Pause (Generic CRM — US)');

    // A later feedback on the same recommendation replaces the message.
    record('Snooze');
    expect(seen.confirmations.get('Alpha')?.get('alpha')).toBe(
      'Snoozed: Pause (Generic CRM — US)',
    );
    expect(seen.confirmations.get('Alpha')?.size).toBe(1);
  });

  it('keeps the same map between renders while its confirmations are unchanged', () => {
    const seen = newSeen();
    const control: { rerender?: () => void } = {};
    function Host(): ReactNode {
      const [, setTick] = useState(0);
      control.rerender = () => {
        setTick((tick) => tick + 1);
      };
      return (
        <>
          <Recorder
            label="Mark done"
            identity="alpha"
            message="Marked done: Pause (Generic CRM — US)"
          />
          <Reader name="Alpha" identities={ALPHA} seen={seen} />
        </>
      );
    }
    render(
      <ActionOutcomeProvider>
        <Host />
      </ActionOutcomeProvider>,
    );
    record('Mark done');
    const recorded = seen.confirmations.get('Alpha');
    const renders = seen.renders.get('Alpha') ?? 0;

    act(() => {
      control.rerender?.();
    });

    expect(seen.renders.get('Alpha')).toBe(renders + 1);
    expect(seen.confirmations.get('Alpha')).toBe(recorded);
  });

  it('reads one confirmation once for an identity listed twice', () => {
    const seen = newSeen();
    render(
      <ActionOutcomeProvider>
        <Recorder
          label="Dismiss"
          identity="alpha"
          message="Dismissed: Pause (Generic CRM — US)"
        />
        <Reader name="Twice" identities={['alpha', 'alpha', 'gamma']} seen={seen} />
      </ActionOutcomeProvider>,
    );

    record('Dismiss');

    expect([...(seen.confirmations.get('Twice') ?? [])]).toEqual([
      ['alpha', 'Dismissed: Pause (Generic CRM — US)'],
    ]);
  });
});

describe('ActionOutcomes: the announcement', () => {
  it('announces a message trimmed in the pre-mounted region, and ignores a blank one', () => {
    function Announcer({ label, message }: { label: string; message: string }): ReactNode {
      const announce = useActionOutcome();
      return (
        <Button
          onClick={() => {
            announce?.(message);
          }}
        >
          {label}
        </Button>
      );
    }
    render(
      <ActionOutcomeProvider>
        <Announcer label="Announce" message="  Snoozed: Pause (Generic CRM — US) " />
        <Announcer label="Announce blank" message="   " />
      </ActionOutcomeProvider>,
    );
    const region = outcomeRegion();
    expect(region.textContent).toBe('');

    fireEvent.click(screen.getByRole('button', { name: 'Announce' }));
    expect(outcomeRegion()).toBe(region);
    expect(region.textContent).toBe('Snoozed: Pause (Generic CRM — US)');
    const node = region.firstChild;

    fireEvent.click(screen.getByRole('button', { name: 'Announce blank' }));
    expect(region.textContent).toBe('Snoozed: Pause (Generic CRM — US)');
    expect(region.firstChild).toBe(node);
    expect(region.getAttribute('role')).toBe('status');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe("ActionOutcomes: the outcome's run view and recommendation", () => {
  /** What a test drives the provider's props and announcer through. */
  interface Control {
    setScope?: (scope: RunId | undefined) => void;
    setIdentities?: (identities: ReadonlySet<string> | undefined) => void;
    announce?: AnnounceActionOutcome | null;
  }

  /** Captures the provider's announcer of the latest render. */
  function Announcer({ control }: { control: Control }): ReactNode {
    control.announce = useActionOutcome();
    return null;
  }

  /**
   * A provider on the default page (`scope` undefined) listing `alpha` and `beta`, whose props
   * `control` changes, with a recorder and a reader for each identity.
   */
  function Host({ control, seen }: { control: Control; seen: Seen }): ReactNode {
    const [scope, setScope] = useState<RunId | undefined>(undefined);
    const [identities, setIdentities] = useState<ReadonlySet<string> | undefined>(
      () => new Set(['alpha', 'beta']),
    );
    control.setScope = setScope;
    control.setIdentities = setIdentities;
    return (
      <ActionOutcomeProvider scope={scope} identities={identities}>
        <Announcer control={control} />
        <Recorder label="Record alpha" identity="alpha" message="Marked done: Alpha" />
        <Recorder label="Record beta" identity="beta" message="Dismissed: Beta" />
        <Reader name="Alpha" identities={ALPHA} seen={seen} />
        <Reader name="Beta" identities={BETA} seen={seen} />
      </ActionOutcomeProvider>
    );
  }

  /** Renders {@link Host} and returns its control and what its readers saw. */
  function renderHost(): { control: Control; seen: Seen } {
    const control: Control = {};
    const seen = newSeen();
    render(<Host control={control} seen={seen} />);
    return { control, seen };
  }

  /** Announces `message` for `identity` through the provider's latest announcer. */
  function announce(control: Control, message: string, identity?: string): void {
    act(() => {
      control.announce?.(message, identity);
    });
  }

  it('empties the same region and clears every confirmation when another run view opens', () => {
    const { control, seen } = renderHost();
    announce(control, 'Marked done: Alpha', 'alpha');
    record('Record alpha');
    record('Record beta');
    const region = outcomeRegion();
    expect(region.textContent).toBe('Marked done: Alpha');
    expect(seen.confirmations.get('Alpha')?.get('alpha')).toBe('Marked done: Alpha');
    // Captured before the switch, as a card's request that settles after it holds it.
    const earlier = control.announce;

    act(() => {
      control.setScope?.(79);
    });

    // In place: the same element, mounted and empty, and still never an alert.
    expect(outcomeRegion()).toBe(region);
    expect(region.isConnected).toBe(true);
    expect(region.getAttribute('role')).toBe('status');
    expect(region.childNodes).toHaveLength(0);
    expect(screen.queryByRole('alert')).toBeNull();
    // Every reader sees an empty map, the listed identities alike.
    expect(seen.confirmations.get('Alpha')).toBe(NO_FEEDBACK_CONFIRMATIONS);
    expect(seen.confirmations.get('Beta')).toBe(NO_FEEDBACK_CONFIRMATIONS);
    // The previous view's announcer announces nothing in this one.
    act(() => {
      earlier?.('Snoozed: Alpha', 'alpha');
    });
    expect(region.childNodes).toHaveLength(0);

    // The region still announces this view's outcomes.
    announce(control, 'Dismissed: Beta', 'beta');
    expect(outcomeRegion()).toBe(region);
    expect(region.textContent).toBe('Dismissed: Beta');

    // Back to the default page: nothing of either view comes back.
    act(() => {
      control.setScope?.(undefined);
    });
    expect(outcomeRegion()).toBe(region);
    expect(region.childNodes).toHaveLength(0);
    expect(seen.confirmations.get('Alpha')).toBe(NO_FEEDBACK_CONFIRMATIONS);
  });

  it('drops the outcome and the confirmation of a recommendation the run no longer lists, and keeps those it lists', () => {
    const { control, seen } = renderHost();
    announce(control, 'Marked done: Alpha', 'alpha');
    record('Record alpha');
    record('Record beta');
    const region = outcomeRegion();
    const beta = seen.confirmations.get('Beta');

    // A new set that still lists both keeps everything.
    act(() => {
      control.setIdentities?.(new Set(['alpha', 'beta', 'gamma']));
    });
    expect(region.textContent).toBe('Marked done: Alpha');
    expect(seen.confirmations.get('Alpha')?.get('alpha')).toBe('Marked done: Alpha');

    // The rerun that follows the feedback no longer lists alpha.
    act(() => {
      control.setIdentities?.(new Set(['beta']));
    });
    expect(outcomeRegion()).toBe(region);
    expect(region.childNodes).toHaveLength(0);
    expect(seen.confirmations.get('Alpha')?.size ?? 0).toBe(0);
    // Beta's confirmation stays, the very map its reader held.
    expect(seen.confirmations.get('Beta')).toBe(beta);
    expect(beta?.get('beta')).toBe('Dismissed: Beta');
    // A confirmation for a recommendation no longer listed is not recorded again.
    record('Record alpha');
    expect(seen.confirmations.get('Alpha')?.size ?? 0).toBe(0);

    // An outcome for a listed recommendation stays; one that names none is not tied to the list.
    announce(control, 'Dismissed: Beta', 'beta');
    expect(region.textContent).toBe('Dismissed: Beta');
    announce(control, 'Decision recorded: Paused');
    act(() => {
      control.setIdentities?.(new Set(['gamma']));
    });
    expect(region.textContent).toBe('Decision recorded: Paused');
    expect(seen.confirmations.get('Beta')?.size ?? 0).toBe(0);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('ties nothing to the run list without identities', () => {
    const { control, seen } = renderHost();
    act(() => {
      control.setIdentities?.(undefined);
    });
    announce(control, 'Marked done: Alpha', 'alpha');
    record('Record alpha');

    expect(outcomeRegion().textContent).toBe('Marked done: Alpha');
    expect(seen.confirmations.get('Alpha')?.get('alpha')).toBe('Marked done: Alpha');
  });
});

describe('FeedbackConfirmation', () => {
  /** The identity the confirmations below carry, shaped as `itemIdentity` builds one. */
  const IDENTITY = '["K1","google","search_term","61000201|7700119|crm \\"demo\\""]';

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
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  });

  /**
   * A card's stand-in: an action row holding one button, which notes, as a card does, that it
   * leaves the page holding focus (`./carriedFocus`).
   */
  function FocusHolder({ label }: { label: string }): ReactNode {
    const rowRef = useRef<HTMLDivElement>(null);
    useFocusLeaving(IDENTITY, () => rowRef.current?.contains(document.activeElement) === true);
    return (
      <div ref={rowRef} data-recommendation-actions="">
        <Button>{label}</Button>
      </div>
    );
  }

  /**
   * What stands in the card's place: the card, its confirmation once the feedback hides it, or
   * nothing once a rerun no longer lists the recommendation.
   */
  type Place = 'card' | 'confirmation' | 'gone';

  /** Options of {@link renderPlace}. */
  interface PlaceOptions {
    /** Wraps the whole in `StrictMode`, which replays every effect on mount, as in development. */
    strict?: boolean;
    /**
     * Whether the next action row stands inside its card's `<article>` ("Next card"), as a
     * recommendation card's does, or outside every card, as a white-paper row's details do.
     */
    nextInCard?: boolean;
  }

  /**
   * Renders, inside one `<section>`, the card stand-in, the confirmation of {@link IDENTITY} in
   * its place, or neither, followed by the next action row, and returns `show`, which changes the
   * place as a refetch does.
   */
  function renderPlace({ strict = false, nextInCard = true }: PlaceOptions = {}): (
    place: Place,
  ) => void {
    const control: { show?: (place: Place) => void } = {};
    function Host(): ReactNode {
      const [place, setPlace] = useState<Place>('card');
      control.show = setPlace;
      return (
        <>
          <Button>Elsewhere</Button>
          <section aria-label="Keywords to adjust">
            {place === 'card' ? <FocusHolder label="Dismiss" /> : null}
            {place === 'confirmation' ? (
              <FeedbackConfirmation
                identity={IDENTITY}
                message="Dismissed: Pause (Generic CRM — US)"
              />
            ) : null}
            {nextInCard ? (
              <article aria-label="Next card">
                <div data-recommendation-actions="">
                  <Button>Next action</Button>
                </div>
              </article>
            ) : (
              <div data-recommendation-actions="">
                <Button>Next action</Button>
              </div>
            )}
          </section>
        </>
      );
    }
    render(
      strict ? (
        <StrictMode>
          <Host />
        </StrictMode>
      ) : (
        <Host />
      ),
    );
    return (place) => {
      act(() => {
        control.show?.(place);
      });
    };
  }

  /** The confirmation's focus target, the element carrying {@link IDENTITY}. */
  function confirmationTarget(): HTMLElement {
    const title = screen.getByText('Dismissed: Pause (Generic CRM — US)');
    const target = title.closest<HTMLElement>('[data-feedback-confirmation]');
    if (target === null) {
      throw new Error('the confirmation has no focus target');
    }
    return target;
  }

  it('renders a success banner titled with the message in an unstyled focus target that carries the identity', () => {
    const { container } = render(
      <FeedbackConfirmation identity={IDENTITY} message="Snoozed: Pause (Generic CRM — US)" />,
    );

    const title = screen.getByText('Snoozed: Pause (Generic CRM — US)');
    const root = container.firstElementChild;
    expect(root?.localName).toBe('div');
    expect(root?.getAttribute('data-feedback-confirmation')).toBe(IDENTITY);
    // Reachable by script only: never a Tab stop, never styled.
    expect(root?.getAttribute('tabindex')).toBe('-1');
    expect(root?.getAttribute('class')).toBeNull();
    expect(root?.getAttribute('style')).toBeNull();
    expect(root?.firstElementChild?.className).toBe('ds-banner ds-banner--success');
    // No live region (the provider announces the outcome), no alert and no control.
    expect(container.querySelector('[role], [aria-live], button, a')).toBeNull();
    expect(title.closest('[role="alert"]')).toBeNull();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('takes the focus of a card of its recommendation that leaves holding it, scrolled into view as far as needed', () => {
    const show = renderPlace();
    act(() => {
      screen.getByRole('button', { name: 'Dismiss' }).focus();
    });

    show('confirmation');

    const target = confirmationTarget();
    expect(document.activeElement).toBe(target);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(target);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
  });

  it('keeps the focus it took under StrictMode, whose replayed effects leave it on the page', async () => {
    const show = renderPlace({ strict: true });
    act(() => {
      screen.getByRole('button', { name: 'Dismiss' }).focus();
    });

    show('confirmation');

    const target = confirmationTarget();
    expect(document.activeElement).toBe(target);
    // A zero-delay timer queued now runs after any recovery the replayed cleanup queued.
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(document.activeElement).toBe(target);
    expect(scrollIntoView.mock.contexts.every((context) => context === target)).toBe(true);

    // It still hands focus on when it does leave the page.
    show('gone');
    const next = screen.getByRole('article', { name: 'Next card' });
    await waitFor(() => {
      expect(document.activeElement).toBe(next);
    });
  });

  it('leaves focus the user has put elsewhere when the card leaves', () => {
    const show = renderPlace();
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' });
    act(() => {
      elsewhere.focus();
    });

    show('confirmation');

    expect(document.activeElement).toBe(elsewhere);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('hands its focus to the card now in its place when it leaves holding it, revealed without a jump', async () => {
    const show = renderPlace();
    show('confirmation');
    act(() => {
      confirmationTarget().focus();
    });
    scrollIntoView.mockClear();

    // A rerun no longer lists the recommendation, so the confirmation leaves the page.
    show('gone');

    // The card itself, never one of its actions, so Enter acts on nothing the user did not choose.
    const next = screen.getByRole('article', { name: 'Next card' });
    await waitFor(() => {
      expect(document.activeElement).toBe(next);
    });
    expect(next.getAttribute('tabindex')).toBe('-1');
    expect(scrollIntoView.mock.contexts).toEqual([next]);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
  });

  it.each([
    { height: 977, block: 'start', fit: 'taller than the viewport, top aligned' },
    { height: 700, block: 'nearest', fit: 'that fits the viewport, as far as needed' },
  ])('hands its focus to a card $fit', async ({ height, block }) => {
    const clientHeight = vi.spyOn(document.documentElement, 'clientHeight', 'get');
    clientHeight.mockReturnValue(800);
    try {
      const show = renderPlace();
      show('confirmation');
      act(() => {
        confirmationTarget().focus();
      });
      scrollIntoView.mockClear();
      // The card spans the viewport from above it, as a tall card does once the confirmation
      // above it leaves: `nearest` would leave its heading out of view.
      const next = screen.getByRole('article', { name: 'Next card' });
      Object.defineProperty(next, 'getBoundingClientRect', {
        configurable: true,
        value: () => new DOMRect(0, -124, 800, height),
      });

      show('gone');

      await waitFor(() => {
        expect(document.activeElement).toBe(next);
      });
      expect(scrollIntoView.mock.contexts).toEqual([next]);
      expect(scrollIntoView).toHaveBeenCalledWith({ block });
    } finally {
      clientHeight.mockRestore();
    }
  });

  it('hands its focus to the first enabled action of a next row outside every card', async () => {
    const show = renderPlace({ nextInCard: false });
    show('confirmation');
    act(() => {
      confirmationTarget().focus();
    });

    show('gone');

    const next = screen.getByRole('button', { name: 'Next action' });
    await waitFor(() => {
      expect(document.activeElement).toBe(next);
    });
  });

  it('moves no focus when it leaves without holding it', async () => {
    const show = renderPlace();
    show('confirmation');
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' });
    act(() => {
      elsewhere.focus();
    });

    show('gone');

    // A zero-delay timer queued now runs after any recovery the removal could have queued.
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(document.activeElement).toBe(elsewhere);
  });
});

describe('ActionOutcomes: a card records its feedback', () => {
  /** The K1 card of the fixture page, inside a provider beside a reader of its identity. */
  async function renderK1(): Promise<{ seen: Seen; identity: string; item: Item }> {
    const item = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.k1);
    const identity = itemIdentity(item);
    const seen = newSeen();
    await renderWithProviders(
      <ActionOutcomeProvider>
        <RecommendationCard item={item} />
        <Reader name="K1" identities={[identity]} seen={seen} />
      </ActionOutcomeProvider>,
      { path: '/actions' },
    );
    return { seen, identity, item };
  }

  it("records the confirmation under the item's identity once the feedback is recorded", async () => {
    mockApi({
      'POST /recommendations/:id/feedback': json(201, makeFeedback('dismiss')),
    });
    const { seen, identity, item } = await renderK1();
    const card = screen.getByRole('article', { name: item.action });
    const message = `Dismissed: ${item.action} (${String(item.entity.name)})`;

    fireEvent.click(within(card).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));

    await waitFor(() => {
      expect(seen.confirmations.get('K1')?.get(identity)).toBe(message);
    });
    // The same text the provider announces.
    expect(outcomeRegion().textContent).toBe(message);
  });

  it('records nothing for a refused feedback', async () => {
    mockApi({
      'POST /recommendations/:id/feedback': apiError(
        500,
        'internal',
        'The feedback was not saved.',
      ),
    });
    const { seen, item } = await renderK1();
    const card = screen.getByRole('article', { name: item.action });

    fireEvent.click(within(card).getByRole('button', { name: FEEDBACK_LABELS.done }));

    await within(card).findByText('The feedback was not saved.');
    expect(seen.confirmations.get('K1')).toBe(NO_FEEDBACK_CONFIRMATIONS);
    expect(outcomeRegion().textContent).toBe('');
  });
});
