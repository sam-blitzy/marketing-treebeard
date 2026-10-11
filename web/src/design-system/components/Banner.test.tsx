/**
 * Contract tests for `Banner`, the global message banner (AAP 0.4.2; `../GAPS.md`, binding
 * procedure step 6).
 *
 * They hold the wrapper to the contract in its TSDoc: the title is a paragraph and never a
 * heading; `role="alert"` appears only with `alert` (AAP 0.5.5: the alert role is used only for
 * discrepancies), and no other banner is a live region; the content and actions containers are
 * skipped for slots React renders as nothing (`undefined`, `null`, booleans, `''`) but kept for
 * `0`; the tone sets exactly one modifier (default `info`); and `id` is forwarded.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { TONES } from '../tokens';
import { Banner } from './Banner';

/** Slot values React renders as nothing, each of which must leave no container behind. */
const EMPTY_SLOTS: readonly [string, ReactNode][] = [
  ['undefined', undefined],
  ['null', null],
  ['false', false],
  ['true', true],
  ['the empty string', ''],
];

/**
 * Returns the banner's root element: the parent of its title paragraph.
 *
 * @param title - the banner's title text
 * @returns the `div.ds-banner` holding that title
 */
function bannerOf(title: string): HTMLElement {
  const root = screen.getByText(title).parentElement;
  if (root === null) {
    throw new Error(`No banner holds the title "${title}"`);
  }
  return root;
}

describe('Banner', () => {
  it('renders the title as a paragraph, never as a heading', () => {
    render(<Banner title="Simulated data: LinkedIn" />);

    const title = screen.getByText('Simulated data: LinkedIn');
    expect(title.tagName).toBe('P');
    expect(title.className).toBe('ds-banner__title');
    expect(screen.queryByRole('heading')).toBeNull();
  });

  it('uses the info tone by default', () => {
    render(<Banner title="Custom window" />);

    expect(bannerOf('Custom window').className).toBe('ds-banner ds-banner--info');
  });

  it.each(TONES)('carries exactly one modifier for the %s tone', (tone) => {
    render(<Banner tone={tone} title="Toned" />);

    const modifiers = Array.from(bannerOf('Toned').classList).filter((name) =>
      name.startsWith('ds-banner--'),
    );
    expect(modifiers).toEqual([`ds-banner--${tone}`]);
  });

  it('is an alert only when alert is set, and announces its title, content and actions', () => {
    render(
      <Banner
        tone="critical"
        title="3 open discrepancies"
        alert
        actions={<a href="#group-campaigns-discrepancies">Review</a>}
      >
        Resolve these first.
      </Banner>,
    );

    const alert = screen.getByRole('alert');
    expect(alert.classList.contains('ds-banner--critical')).toBe(true);
    expect(within(alert).getByText('3 open discrepancies').className).toBe('ds-banner__title');
    expect(within(alert).getByText('Resolve these first.').className).toBe('ds-banner__content');
    const review = within(alert).getByRole('link', { name: 'Review' });
    expect(review.getAttribute('href')).toBe('#group-campaigns-discrepancies');
    expect(review.parentElement?.className).toBe('ds-banner__actions');
    expect(alert.textContent).toBe('3 open discrepanciesResolve these first.Review');
  });

  it('is silent content without alert: no alert, status, live region or landmark', () => {
    render(<Banner tone="warning" title="Stale data" />);

    const banner = bannerOf('Stale data');
    expect(banner.hasAttribute('role')).toBe(false);
    expect(banner.hasAttribute('aria-live')).toBe(false);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('region')).toBeNull();
  });

  it('treats alert={false} like an omitted alert', () => {
    render(<Banner alert={false} title="Short window" />);

    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders supporting content and operable actions in their own containers, in order', () => {
    const review = vi.fn();
    render(
      <Banner
        title="Open discrepancies"
        actions={
          <button type="button" onClick={review}>
            Review discrepancies
          </button>
        }
      >
        Two campaigns differ from their decisions.
      </Banner>,
    );

    const banner = bannerOf('Open discrepancies');
    expect(Array.from(banner.children).map((child) => child.className)).toEqual([
      'ds-banner__title',
      'ds-banner__content',
      'ds-banner__actions',
    ]);
    expect(banner.querySelector('.ds-banner__content')?.textContent).toBe(
      'Two campaigns differ from their decisions.',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Review discrepancies' }));
    expect(review).toHaveBeenCalledTimes(1);
  });

  it.each(EMPTY_SLOTS)('renders no content or actions container for %s', (_name, slot) => {
    render(
      <Banner title="Empty slots" actions={slot}>
        {slot}
      </Banner>,
    );

    const banner = bannerOf('Empty slots');
    expect(banner.children).toHaveLength(1);
    expect(banner.querySelector('.ds-banner__content')).toBeNull();
    expect(banner.querySelector('.ds-banner__actions')).toBeNull();
  });

  it('renders 0 in both slots, because 0 is content', () => {
    render(
      <Banner title="Zero" actions={0}>
        {0}
      </Banner>,
    );

    const banner = bannerOf('Zero');
    expect(banner.querySelector('.ds-banner__content')?.textContent).toBe('0');
    expect(banner.querySelector('.ds-banner__actions')?.textContent).toBe('0');
  });

  it('forwards its id, so an in-page link can target it', () => {
    render(<Banner id="banner-discrepancies" title="Discrepancies" />);

    expect(bannerOf('Discrepancies').id).toBe('banner-discrepancies');
  });

  it('renders no id attribute when none is given', () => {
    render(<Banner title="No id" />);

    expect(bannerOf('No id').hasAttribute('id')).toBe(false);
  });

  it('breaks long identifiers of a string title and content at their underscores only', () => {
    const title =
      'No CRM link for Google campaigns: HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured';
    const content = 'Set HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY, then sync HubSpot.';
    const { container } = render(<Banner title={title}>{content}</Banner>);

    const titleNode = container.querySelector('.ds-banner__title');
    const contentNode = container.querySelector('.ds-banner__content');
    // The text, and so the banner's accessible text, is exactly the API's.
    expect(titleNode?.textContent).toBe(title);
    expect(contentNode?.textContent).toBe(content);
    expect(titleNode?.querySelectorAll('wbr')).toHaveLength(3);
    expect(contentNode?.querySelectorAll('wbr')).toHaveLength(4);
    for (const wbr of container.querySelectorAll('wbr')) {
      expect(wbr.previousSibling?.textContent?.endsWith('_')).toBe(true);
    }
  });

  it('leaves a title without a long identifier, and element content, as given', () => {
    const { container } = render(
      <Banner title="Simulated data: Google (DATA_MODE=simulated)">
        <em>HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY</em>
      </Banner>,
    );

    expect(container.querySelector('.ds-banner__title')?.innerHTML).toBe(
      'Simulated data: Google (DATA_MODE=simulated)',
    );
    expect(container.querySelector('.ds-banner__content')?.innerHTML).toBe(
      '<em>HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY</em>',
    );
  });
});
