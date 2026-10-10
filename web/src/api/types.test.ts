// @vitest-environment node
/**
 * Contract-type tests for src/api/types.ts, the mirror of
 * `backend/src/ads_engine/api/schemas.py` (AAP 0.5.3).
 *
 * The assertions are compile-time: `npm run typecheck` (`tsc --noEmit`) compiles this file, so
 * it pins the mirrored types in `types.ts` to the wire shapes written out here by hand from
 * `schemas.py`. A change to `types.ts` that departs from them fails the typecheck, and an
 * `@ts-expect-error` that no longer meets an error fails it as well; a change to `schemas.py`
 * alone goes unseen until the mirror and this file's expected type are updated with it. Vitest
 * runs the same assertions as no-ops.
 */
import { describe, expectTypeOf, it } from 'vitest';

import type { SampleSize, WithheldSample } from './types';

/**
 * The values `schemas.SampleSize` (`int | str | None`) puts on the wire: an integer for a
 * whole-number size, a fixed-point decimal string for a fractional one, `null` when no sample
 * applies.
 */
type WireSampleSize = number | string | null;

describe('WithheldSample (schemas.WithheldSample)', () => {
  it('types both sizes as schemas.SampleSize', () => {
    expectTypeOf<SampleSize>().toEqualTypeOf<WireSampleSize>();
    expectTypeOf<WithheldSample['observed']>().toEqualTypeOf<WireSampleSize>();
    expectTypeOf<WithheldSample['required']>().toEqualTypeOf<WireSampleSize>();
  });

  it('refuses a boolean size, which the API never sends', () => {
    const sample: WithheldSample = {
      // @ts-expect-error -- schemas.SampleSize has no boolean member, so neither has SampleSize.
      observed: true,
      // @ts-expect-error -- schemas.SampleSize has no boolean member, so neither has SampleSize.
      required: false,
      unit: 'variants',
    };
    expectTypeOf(sample).toEqualTypeOf<WithheldSample>();
  });
});
