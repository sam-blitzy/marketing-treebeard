/**
 * Component tests for `StatusBar`, the status bar at the top of `/actions` (AAP 0.5.5): a region
 * named "Analysis status" holding, in order, the window picker, each source's day boundary from
 * `Run.source_days` with the display time zone, and the source chips.
 *
 * The bar's children read `GET /config`, `GET /runs/latest` and `GET /sources` through the real
 * hooks, answered by `../../test/mockApi` with payloads from `../../test/fixtures`; the picker
 * reads `GET /runs?kind=custom` only once its recent custom windows are opened, so that read has
 * no handler and would fail the test. `mockApi` is imported before `../../test/render` and
 * Testing Library, so its unmatched-request check runs after Testing Library's cleanup.
 */
import { json, mockApi } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Run } from '../../api/types';
import { formatSourceBoundaries } from '../../lib/format';
import {
  FIXTURE_SOURCE_DAYS,
  makeConfig,
  makeOverlay,
  makeRun,
  makeSources,
} from '../../test/fixtures';
import { StatusBar } from './StatusBar';

/** Stubs every read the bar's children make, with the fixture world. */
function stubReads(): void {
  mockApi({
    'GET /config': json(200, makeConfig()),
    'GET /runs/latest': json(200, makeRun()),
    'GET /sources': json(200, makeSources()),
  });
}

/** Renders the bar for `run` and waits until the picker's presets and the chips' syncs show. */
async function renderBar(run: Run = makeRun()) {
  stubReads();
  const result = await renderWithProviders(<StatusBar run={run} overlay={makeOverlay()} />);
  await waitFor(() => {
    expect(screen.getByRole('button', { name: '28 days (default)' })).toBeTruthy();
    expect(screen.getAllByText(/^Last sync /u)).toHaveLength(3);
  });
  return result;
}

/** The status region. */
function statusRegion(): HTMLElement {
  return screen.getByRole('region', { name: 'Analysis status' });
}

describe('StatusBar', () => {
  it('holds the window picker, the day boundaries and the source chips, in that order', async () => {
    await renderBar();

    const region = statusRegion();
    expect(region.tagName).toBe('SECTION');
    const picker = within(region).getByRole('group', { name: 'Analysis window' });
    const boundaries = within(region).getByText(
      'Google days: America/New_York · LinkedIn days: UTC',
    );
    const chips = within(region).getByRole('list', { name: 'Data sources' });

    expect(picker.compareDocumentPosition(boundaries) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(boundaries.compareDocumentPosition(chips) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    // The run on screen is the default run, so the default preset is pressed.
    expect(
      within(picker).getByRole('button', { name: '28 days (default)' }).getAttribute('aria-pressed'),
    ).toBe('true');
    expect(within(chips).getAllByRole('listitem')).toHaveLength(3);
  });

  it('shows the boundary line formatSourceBoundaries builds from the run’s source days', async () => {
    const run = makeRun();
    await renderBar(run);

    const expected = formatSourceBoundaries(run.source_days);
    // The default fixture is AAP 0.5.5's own example: Google on its account zone, LinkedIn on UTC.
    expect(expected).toBe('Google days: America/New_York · LinkedIn days: UTC');
    expect(within(statusRegion()).getByText(expected)).toBeTruthy();
  });

  it('follows the run’s data when a source’s day boundary changes', async () => {
    const run = makeRun({
      source_days: FIXTURE_SOURCE_DAYS.map((day) =>
        day.source === 'google' ? { ...day, boundary: 'Europe/Berlin' } : { ...day },
      ),
    });
    await renderBar(run);

    const region = statusRegion();
    const expected = formatSourceBoundaries(run.source_days);
    expect(expected).toContain('Google days: Europe/Berlin');
    expect(within(region).getByText(expected)).toBeTruthy();
    // No zone is kept from the default fixture: the line is built from this run alone.
    expect(within(region).queryByText(/America\/New_York/u)).toBeNull();
    expect(within(region).getByText('Day boundaries differ between sources')).toBeTruthy();
  });

  it('names the run’s display time zone, whatever it is', async () => {
    await renderBar(makeRun({ display_timezone: 'America/Chicago' }));

    const region = statusRegion();
    expect(within(region).getByText('Times shown in America/Chicago')).toBeTruthy();
    // The chips' instants use the same zone, and it is no source's day boundary.
    expect(within(region).getAllByText(/^Last sync .+ America\/Chicago$/u)).toHaveLength(3);
    expect(within(region).queryByText(/Chicago days/u)).toBeNull();
  });

  it('says when the sources end their days at different boundaries, and names the display time zone', async () => {
    await renderBar();

    const region = statusRegion();
    expect(within(region).getByText('Day boundaries differ between sources')).toBeTruthy();
    expect(within(region).getByText('Times shown in UTC')).toBeTruthy();
  });

  it('leaves the difference note out when every source shares one boundary', async () => {
    const run = makeRun({
      source_days: [
        { source: 'google', start: '2026-09-08', end: '2026-10-05', boundary: 'UTC' },
        { source: 'linkedin', start: '2026-09-08', end: '2026-10-05', boundary: 'UTC' },
      ],
    });
    await renderBar(run);

    const region = statusRegion();
    expect(within(region).getByText('Google days: UTC · LinkedIn days: UTC')).toBeTruthy();
    expect(within(region).queryByText('Day boundaries differ between sources')).toBeNull();
  });

  it('shows no boundary line for a run without source days, and still names the time zone', async () => {
    await renderBar(makeRun({ source_days: [] }));

    const region = statusRegion();
    expect(within(region).queryByText(/ days: /u)).toBeNull();
    expect(within(region).queryByText('Day boundaries differ between sources')).toBeNull();
    expect(within(region).getByText('Times shown in UTC')).toBeTruthy();
  });

  it('formats every instant of the bar in the run’s display time zone', async () => {
    await renderBar(makeRun({ display_timezone: 'America/New_York' }));

    const region = statusRegion();
    expect(within(region).getByText('Times shown in America/New_York')).toBeTruthy();
    expect(
      within(region).getAllByText('Last sync Oct 6, 2026, 1:30 AM America/New_York'),
    ).toHaveLength(3);
  });

  it('announces nothing as an alert', async () => {
    await renderBar();

    expect(screen.queryByRole('alert')).toBeNull();
  });
});
