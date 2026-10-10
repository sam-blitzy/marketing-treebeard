/**
 * Read-state helpers shared by the two lists of `/decisions`, `DiscrepancyResolver` and
 * `DecisionLog` (AAP 0.1.1 accessibility: WCAG 2.2 AA).
 *
 * Both lists announce their load outcomes through one polite status region they keep mounted
 * from the first render (WCAG 2.2 SC 4.1.3), offer "Retry" on a failed read, and keep a list
 * they already loaded on screen when a background refetch fails, saying it is the state as last
 * loaded. These helpers hold the parts of that behaviour the two lists share:
 *
 * - {@link useSettledFailure}: the failure of a read that holds no data, kept while the same
 *   read runs again, so polling and Retry never flicker the failure back to the spinner;
 * - {@link useRetry}: the "Retry" button's loading state and the focus return once it settles
 *   (WCAG 2.2 SC 2.4.3);
 * - {@link lastLoadedText}: when the list on screen was last loaded, in the display time zone.
 *
 * They follow the same patterns as `features/actions/ActionsPage.tsx` and
 * `features/sources/SourcesPage.tsx`. Server state still goes only through
 * `../../api/queries` (AAP 0.11.1); nothing here fetches.
 *
 * @packageDocumentation
 */
import { useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';

import type { ApiError } from '../../api/queries';
import { formatInstant } from '../../lib/format';

/** The label of the button that repeats a failed list read. */
export const RETRY_LABEL = 'Retry';

/**
 * The error a list read last settled with while it holds no data, kept while the same read runs
 * again.
 *
 * TanStack Query v5 puts a query that holds no data back to `status: 'pending'` with
 * `error: null` each time it fetches. The lists poll, so without this hook every poll after a
 * failed first read, and every Retry, would swap the failure for the loading spinner and back:
 * the screen would flicker, its Retry button and keyboard focus with it would be replaced, and
 * the status region would announce the failure again each time (WCAG 2.2 SC 2.4.3, 4.1.3). The
 * failure is kept until the read settles again, with a new error that replaces it or with data
 * that clears it.
 *
 * The state is adjusted while rendering, React's pattern for information kept from earlier
 * renders; the guard makes the second render a no-op.
 *
 * @param error - the query's `error`
 * @param fetching - whether the query is fetching or paused waiting for the network
 * @param hasData - whether the query holds data
 * @returns the failure to present, or `null` while there is data, before any read failed, or
 *   once a read settled without an error
 */
export function useSettledFailure(
  error: ApiError | null,
  fetching: boolean,
  hasData: boolean,
): ApiError | null {
  const [kept, setKept] = useState<ApiError | null>(null);
  const settled = hasData ? null : error === null && fetching ? kept : error;
  if (settled !== kept) {
    setKept(settled);
  }
  return settled;
}

/** What {@link useRetry} returns. */
export interface RetryControl {
  /** Whether a Retry the user started is still running; pass it to the button's `loading`. */
  retrying: boolean;
  /** The Retry button, so focus can return to it. */
  buttonRef: RefObject<HTMLButtonElement | null>;
  /**
   * Runs the retry; call it from the button's click handler. It rejects only when the `refetch`
   * given to {@link useRetry} rejects, with that rejection, and clears the loading state either
   * way. TanStack Query's `refetch`, which both lists pass, never rejects, so with it the retry
   * never rejects either.
   */
  retry: () => Promise<void>;
}

/**
 * The "Retry" button of a failed list read: its loading state while the read runs again, and
 * focus returned to it once the read settles.
 *
 * A design-system `Button` is natively disabled while `loading`, and a disabled button drops
 * focus to the page body. Once the retry settles, focus goes back to the button if it is still
 * on the page and enabled and focus has not been moved elsewhere in the meantime (WCAG 2.2
 * SC 2.4.3). A read that succeeds removes the button, and focus is then left where it is. A
 * background poll never moves focus, because only a Retry the user started arms the return.
 *
 * @param refetch - the query's `refetch`; TanStack's never rejects, a failed read settles into
 *   the query's error instead
 * @returns the button's loading state, its ref and the retry to run on click
 */
export function useRetry(refetch: () => Promise<unknown>): RetryControl {
  const [retrying, setRetrying] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const armedRef = useRef(false);

  useEffect(() => {
    if (retrying || !armedRef.current) {
      return;
    }
    armedRef.current = false;
    const button = buttonRef.current;
    const active = document.activeElement;
    if (
      button !== null &&
      button.isConnected &&
      !button.disabled &&
      (active === null || active === document.body)
    ) {
      button.focus();
    }
  }, [retrying]);

  const retry = async (): Promise<void> => {
    armedRef.current = true;
    setRetrying(true);
    try {
      await refetch();
    } finally {
      setRetrying(false);
    }
  };

  return { retrying, buttonRef, retry };
}

/**
 * When the list on screen was last loaded: the query's `dataUpdatedAt` as an instant in the
 * display time zone, labelled with that zone by `formatInstant` (AAP 0.5.5 "Instants are
 * formatted in `DISPLAY_TIMEZONE`").
 *
 * @param dataUpdatedAt - the query's `dataUpdatedAt`, milliseconds since the epoch
 * @param displayTimezone - the IANA zone instants are shown in
 * @returns the formatted instant, for example "Oct 6, 2026, 3:00 AM UTC"
 */
export function lastLoadedText(dataUpdatedAt: number, displayTimezone: string): string {
  return formatInstant(new Date(dataUpdatedAt).toISOString(), displayTimezone);
}
