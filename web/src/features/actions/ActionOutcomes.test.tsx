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
 * - `FeedbackConfirmation`: a success banner titled with the message, never a live region, an
 *   alert or a Tab stop;
 * - a card records its feedback's confirmation, under its item's identity, only once the
 *   feedback is recorded: a refused feedback leaves none.
 *
 * The store tests render plain components; the card test stubs only `fetch`.
 */
import { apiError, json, mockApi } from '../../test/mockApi';

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

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
  it('renders a success banner titled with the message, as plain content', () => {
    const { container } = render(
      <FeedbackConfirmation message="Snoozed: Pause (Generic CRM — US)" />,
    );

    const title = screen.getByText('Snoozed: Pause (Generic CRM — US)');
    expect(container.firstElementChild?.className).toBe('ds-banner ds-banner--success');
    expect(container.querySelector('[role], [aria-live], [tabindex], button, a')).toBeNull();
    expect(title.closest('[role="alert"]')).toBeNull();
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
