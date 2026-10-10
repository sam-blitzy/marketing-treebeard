/**
 * Per-test timeouts of the web component tests, for the few tests whose work legitimately
 * outlasts Vitest's default 5,000 ms budget on a busy host.
 *
 * Only test files import this module; application code never does. It imports nothing.
 *
 * @example
 * ```tsx
 * import { LARGE_RENDER_TEST_TIMEOUT_MS } from '../../../test/timeouts';
 *
 * it('mounts 250 visible cards one batch at a time', () => {
 *   // … render hundreds of cards and reveal them batch by batch …
 * }, LARGE_RENDER_TEST_TIMEOUT_MS);
 * ```
 *
 * @packageDocumentation
 */

/**
 * The budget, in milliseconds, of a deliberately large bounded-batch render: a test that renders
 * dozens to hundreds of cards or table rows and reveals them one bounded batch at a time. Such a
 * test takes under two seconds alone, but under `npm test` (coverage on, every test file in
 * parallel) on a loaded host it can outlast Vitest's default 5,000 ms, although nothing in it is
 * slow or stuck.
 */
export const LARGE_RENDER_TEST_TIMEOUT_MS = 30_000;
