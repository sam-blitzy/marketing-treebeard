/**
 * Component tests for `SourceChips`, the per-source chips of the `/actions` status bar
 * (AAP 0.5.5), and for its helper `latestSuccess`.
 *
 * For Google, LinkedIn and HubSpot, in that order, each item shows the source's name, its mode
 * from the read-time overlay (Live, Simulated, or "No data yet" before its first dataset), a
 * Stale and an Incomplete chip when a dataset of that source is stale or not complete now, and
 * its last successful sync from `GET /sources`, formatted in the display time zone the caller
 * passes.
 *
 * `GET /sources` is answered by `../../test/mockApi`, so the real client and `useSources` hook
 * run; payloads come from `../../test/fixtures`. `mockApi` is imported before `../../test/render`
 * and Testing Library, so its unmatched-request check runs after Testing Library's cleanup.
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { queryKeys } from '../../api/queries';
import type { DatasetFreshnessView, Overlay, Source } from '../../api/types';
import { makeOverlay, makeSource, makeSources } from '../../test/fixtures';
import { SOURCE_CHIP_ORDER, SourceChips, latestSuccess } from './SourceChips';

/** "Last sync" of the fixture's 05:30 UTC sync cycle, in UTC. */
const LAST_SYNC_UTC = 'Last sync Oct 6, 2026, 5:30 AM UTC';

/** The list items of the chips, in document order. */
function chipItems(): HTMLElement[] {
  return within(screen.getByRole('list', { name: 'Data sources' })).getAllByRole('listitem');
}

/** The list item of one source, found by the name it starts with. */
function chipItem(name: string): HTMLElement {
  const item = chipItems().find((candidate) => candidate.textContent?.startsWith(name) === true);
  if (item === undefined) {
    throw new Error(`No source chip starts with "${name}".`);
  }
  return item;
}

/** The chip texts of one item, in document order. */
function badges(item: HTMLElement): string[] {
  return Array.from(item.querySelectorAll('.ds-badge'), (badge) => badge.textContent ?? '');
}

/** A freshness row of `dataset` with `last_success_at`. */
function freshnessRow(dataset: string, lastSuccessAt: string | null): DatasetFreshnessView {
  const [row] = makeSource('google').datasets;
  if (row === undefined) {
    throw new Error('The fixture source has no freshness row.');
  }
  return { ...row, dataset, last_success_at: lastSuccessAt };
}

/** Renders the chips for `overlay` in `displayTimezone`, at `/actions`. */
async function renderChips(overlay: Overlay = makeOverlay(), displayTimezone = 'UTC') {
  return renderWithProviders(
    <SourceChips overlay={overlay} displayTimezone={displayTimezone} />,
  );
}

describe('SOURCE_CHIP_ORDER', () => {
  it('orders the sources Google, LinkedIn, HubSpot, frozen', () => {
    expect(SOURCE_CHIP_ORDER).toEqual(['google', 'linkedin', 'hubspot']);
    expect(Object.isFrozen(SOURCE_CHIP_ORDER)).toBe(true);
  });
});

describe('latestSuccess', () => {
  it('returns the newest success of the active primary dataset, exactly as sent', () => {
    expect(latestSuccess(makeSource('google'))).toBe('2026-10-06T05:30:00Z');
  });

  it('compares instants by time, not as strings, across UTC offsets', () => {
    const source = makeSource('linkedin', {
      datasets: [
        freshnessRow('linkedin.entities', '2026-10-06T05:30:00Z'),
        // 06:00 UTC: later in time, although it sorts before the first as a string.
        freshnessRow('linkedin.leads', '2026-10-06T08:00:00+02:00'),
        freshnessRow('linkedin.segments', '2026-10-06T07:00:00+02:00'),
      ],
    });

    expect(latestSuccess(source)).toBe('2026-10-06T08:00:00+02:00');
  });

  it('skips rows with no success yet or an unparsable instant, and keeps the first of equal instants', () => {
    const source = makeSource('hubspot', {
      datasets: [
        freshnessRow('hubspot.google_contacts', null),
        freshnessRow('hubspot.contacts', 'not an instant'),
        freshnessRow('hubspot.associations', '2026-10-06T03:00:00Z'),
        freshnessRow('hubspot.deal_search', '2026-10-06T05:00:00+02:00'),
      ],
    });

    expect(latestSuccess(source)).toBe('2026-10-06T03:00:00Z');
  });

  it('returns null without the source, without an active dataset or without any success', () => {
    expect(latestSuccess(undefined)).toBeNull();
    expect(latestSuccess(makeSource('google', { active_dataset: null }))).toBeNull();
    expect(
      latestSuccess(
        makeSource('google', { datasets: [freshnessRow('google.entities', null)] }),
      ),
    ).toBeNull();
  });

  it('never reads the companion CRM dataset', () => {
    const hubspot = makeSource('hubspot', {
      datasets: [freshnessRow('hubspot.contacts', '2026-10-05T01:00:00Z')],
      companion_datasets: [freshnessRow('hubspot.contacts', '2026-10-06T09:00:00Z')],
    });

    expect(latestSuccess(hubspot)).toBe('2026-10-05T01:00:00Z');
  });
});

describe('SourceChips', () => {
  it('lists Google, LinkedIn and HubSpot in that order, each with its mode and last sync', async () => {
    mockApi({ 'GET /sources': json(200, makeSources()) });
    await renderChips();

    await waitFor(() => {
      expect(screen.getAllByText(LAST_SYNC_UTC)).toHaveLength(3);
    });
    const items = chipItems();
    expect(items.map((item) => item.firstElementChild?.firstElementChild?.textContent)).toEqual([
      'Google',
      'LinkedIn',
      'HubSpot',
    ]);
    expect(items.map(badges)).toEqual([['Simulated', 'Incomplete'], ['Simulated'], ['Simulated']]);
  });

  it('shows Live, Simulated and "No data yet" from the overlay, never from the sources list', async () => {
    mockApi({ 'GET /sources': json(200, makeSources()) });
    await renderChips(
      makeOverlay({
        source_modes: { google: 'live', linkedin: 'simulated', hubspot: null },
        incomplete_datasets: [],
      }),
    );

    // GET /sources reports every source simulated; the overlay's current modes win.
    expect(badges(chipItem('Google'))).toEqual(['Live']);
    expect(badges(chipItem('LinkedIn'))).toEqual(['Simulated']);
    expect(badges(chipItem('HubSpot'))).toEqual(['No data yet']);
    expect(chipItem('Google').querySelector('.ds-badge--success')?.textContent).toBe('Live');
    expect(chipItem('LinkedIn').querySelector('.ds-badge--info')?.textContent).toBe('Simulated');
    expect(chipItem('HubSpot').querySelector('.ds-badge--neutral')?.textContent).toBe(
      'No data yet',
    );
    await waitFor(() => {
      expect(screen.getAllByText(LAST_SYNC_UTC)).toHaveLength(3);
    });
  });

  it('adds Stale and Incomplete only to the sources whose datasets carry them', async () => {
    mockApi({ 'GET /sources': json(200, makeSources()) });
    await renderChips(
      makeOverlay({
        stale_datasets: ['linkedin.leads', 'hubspotish.contacts'],
        incomplete_datasets: ['google.search_terms', 'linkedin.segments', 'googlex.entities'],
      }),
    );

    expect(badges(chipItem('Google'))).toEqual(['Simulated', 'Incomplete']);
    expect(badges(chipItem('LinkedIn'))).toEqual(['Simulated', 'Stale', 'Incomplete']);
    // "hubspotish." is not HubSpot's "hubspot." prefix.
    expect(badges(chipItem('HubSpot'))).toEqual(['Simulated']);
    expect(chipItem('LinkedIn').querySelectorAll('.ds-badge--warning')).toHaveLength(2);
    await waitFor(() => {
      expect(screen.getAllByText(LAST_SYNC_UTC)).toHaveLength(3);
    });
  });

  it('formats the last sync in the display time zone it is given', async () => {
    mockApi({ 'GET /sources': json(200, makeSources()) });
    await renderChips(makeOverlay(), 'America/New_York');

    await waitFor(() => {
      expect(
        within(chipItem('Google')).getByText('Last sync Oct 6, 2026, 1:30 AM America/New_York'),
      ).toBeTruthy();
    });
  });

  it('shows no last sync until GET /sources answers', async () => {
    let answer: (response: MockResponse) => void = () => {
      throw new Error('GET /sources was not requested.');
    };
    const api = mockApi({
      'GET /sources': () =>
        new Promise<MockResponse>((resolve) => {
          answer = resolve;
        }),
    });
    await renderChips();

    await waitFor(() => {
      expect(api.calls.some((call) => call.path === '/sources')).toBe(true);
    });
    expect(chipItems()).toHaveLength(3);
    expect(screen.queryByText(/Last sync/u)).toBeNull();
    expect(screen.queryByText('Never synced')).toBeNull();

    answer(json(200, makeSources()));
    await waitFor(() => {
      expect(screen.getAllByText(LAST_SYNC_UTC)).toHaveLength(3);
    });
  });

  it('says "Never synced" for a source with no success, no active dataset or no entry', async () => {
    const sources: Source[] = [
      makeSource('google', { datasets: [freshnessRow('google.entities', null)] }),
      makeSource('linkedin', { active_dataset: null }),
    ];
    mockApi({ 'GET /sources': json(200, sources) });
    await renderChips();

    await waitFor(() => {
      expect(screen.getAllByText('Never synced')).toHaveLength(3);
    });
    expect(screen.queryByText(/Last sync/u)).toBeNull();
  });

  it('says "Last sync unknown" when GET /sources fails before any answer', async () => {
    mockApi({
      'GET /sources': apiError(500, 'internal_error', 'The server failed to list sources.'),
    });
    await renderChips();

    await waitFor(() => {
      expect(screen.getAllByText('Last sync unknown')).toHaveLength(3);
    });
    // The failure is stated in place, never as an alert.
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('keeps the last answer on screen when a later poll fails', async () => {
    const api = mockApi({ 'GET /sources': json(200, makeSources()) });
    const { queryClient } = await renderChips();
    await waitFor(() => {
      expect(screen.getAllByText(LAST_SYNC_UTC)).toHaveLength(3);
    });

    api.use({ 'GET /sources': apiError(503, 'unavailable', 'The API is restarting.') });
    await queryClient.refetchQueries({ queryKey: queryKeys.sources });

    expect(queryClient.getQueryState(queryKeys.sources)?.status).toBe('error');
    expect(api.calls.filter((call) => call.path === '/sources')).toHaveLength(2);
    expect(screen.getAllByText(LAST_SYNC_UTC)).toHaveLength(3);
    expect(screen.queryByText('Last sync unknown')).toBeNull();
  });
});
