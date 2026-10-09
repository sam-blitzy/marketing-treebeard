/**
 * Typed mirror of the semantic design tokens declared in `tokens.css`.
 *
 * The token names are the stable contract for feature code and for the design-system
 * wrappers: they are listed one-to-one, in the order `tokens.css` declares them, by
 * {@link TOKEN_NAMES}, and every value in {@link tokens} is a `var(--…)` reference to one of
 * those names. This module holds no design values. The values are neutral placeholders that
 * live only in `tokens.css`, so binding the Blitzy OS values replaces that stylesheet and never
 * touches this file. Every token is a gap until then, as listed in `GAPS.md`.
 *
 * The `Grid` breakpoint is intentionally absent: CSS custom properties cannot be used in
 * `@media` conditions, so a breakpoint token could never be read where it is needed. Its value
 * lives in `components/Grid.css` and is recorded in `GAPS.md`.
 *
 * Usage, inside the design-system components only (feature code imports the design-system
 * index and styles through component props, never through raw values):
 *
 * ```ts
 * import { tokens, type SpaceStep } from '../tokens';
 *
 * const gapVar = (gap: SpaceStep) => tokens.space[gap]; // 'var(--space-1)' … 'var(--space-8)'
 * ```
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */

/**
 * Every semantic CSS custom property, in `tokens.css` declaration order (AAP 0.4.3): ten
 * colours, eight spacing steps, the font family, five font sizes, two font weights, two line
 * heights, two radii, two shadows, three z-index layers, two opacities and two sizes. The
 * opacities and the sizes extend the AAP 0.4.3 list, so that every component value, including
 * disabled and backdrop opacity and full-extent geometry, resolves to a token (AAP 0.4.4).
 *
 * `tokens.test.ts` holds `tokens.css` to this exact list and order, so a token added, renamed
 * or removed in one place and not the other fails the tests.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export const TOKEN_NAMES = [
  '--color-bg',
  '--color-surface',
  '--color-border',
  '--color-text',
  '--color-text-muted',
  '--color-accent',
  '--color-info',
  '--color-warning',
  '--color-critical',
  '--color-success',
  '--space-1',
  '--space-2',
  '--space-3',
  '--space-4',
  '--space-5',
  '--space-6',
  '--space-7',
  '--space-8',
  '--font-family',
  '--font-size-xs',
  '--font-size-sm',
  '--font-size-md',
  '--font-size-lg',
  '--font-size-xl',
  '--font-weight-regular',
  '--font-weight-semibold',
  '--line-height-tight',
  '--line-height-normal',
  '--radius-sm',
  '--radius-md',
  '--shadow-1',
  '--shadow-2',
  '--z-banner',
  '--z-dialog',
  '--z-tooltip',
  '--opacity-disabled',
  '--opacity-backdrop',
  '--size-full',
  '--size-container-full',
] as const;

/**
 * The name of one semantic CSS custom property, for example `'--color-text'`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export type TokenName = (typeof TOKEN_NAMES)[number];

/** A `var()` reference to a declared token: the only value shape {@link tokens} may hold. */
type TokenVar = `var(${TokenName})`;

/**
 * Returns the `var()` reference for a token, typed to the exact token it names.
 *
 * Every value in {@link tokens} is built through this function, so a mistyped token name is a
 * compile error rather than a silently unset CSS property. The result carries no fallback
 * value: a fallback would be a second, hardcoded design value (AAP 0.4.4).
 *
 * @param name - a token declared in `tokens.css`
 * @returns `var(<name>)`, for example `cssVar('--color-text')` returns `'var(--color-text)'`
 */
export function cssVar<const N extends TokenName>(name: N): `var(${N})` {
  return `var(${name})`;
}

/**
 * The spacing scale steps accepted by the layout primitives' `gap` props (`Stack`, `Inline`,
 * `Grid`). Step `n` resolves to the `--space-n` token through `tokens.space[n]`; the step is an
 * index into the scale, not a size.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export const SPACE_STEPS = [1, 2, 3, 4, 5, 6, 7, 8] as const;

/**
 * One step of the spacing scale, `1` to `8`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export type SpaceStep = (typeof SPACE_STEPS)[number];

/**
 * The semantic tones shared by `Badge` and `Banner` (AAP 0.4.2).
 *
 * `neutral` maps to the text and border colours (`tokens.color.text`, `tokens.color.border`).
 * Every other tone maps to the colour token of the same name, `--color-{tone}`
 * (`tokens.color.info`, `tokens.color.warning`, `tokens.color.critical`,
 * `tokens.color.success`). A tone conveys meaning only together with its text label, never
 * through colour alone.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export const TONES = ['neutral', 'info', 'warning', 'critical', 'success'] as const;

/**
 * One semantic tone: `'neutral' | 'info' | 'warning' | 'critical' | 'success'`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export type Tone = (typeof TONES)[number];

/**
 * Typed `var()` references for every semantic token, grouped by category (AAP 0.4.3).
 *
 * Each leaf has the literal type of its reference, for example `tokens.color.text` is
 * `'var(--color-text)'`, and every leaf is built by {@link cssVar} from a {@link TokenName}.
 * `tokens.space` is keyed by {@link SpaceStep}, so `tokens.space[step]` is a defined
 * `var(--space-n)` reference for every step, even under `noUncheckedIndexedAccess`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export const tokens = {
  /** Colour tokens. GAP: bind to Blitzy OS on source arrival. */
  color: {
    bg: cssVar('--color-bg'),
    surface: cssVar('--color-surface'),
    border: cssVar('--color-border'),
    text: cssVar('--color-text'),
    textMuted: cssVar('--color-text-muted'),
    accent: cssVar('--color-accent'),
    info: cssVar('--color-info'),
    warning: cssVar('--color-warning'),
    critical: cssVar('--color-critical'),
    success: cssVar('--color-success'),
  },
  /** Spacing scale, keyed by {@link SpaceStep}. GAP: bind to Blitzy OS on source arrival. */
  space: {
    1: cssVar('--space-1'),
    2: cssVar('--space-2'),
    3: cssVar('--space-3'),
    4: cssVar('--space-4'),
    5: cssVar('--space-5'),
    6: cssVar('--space-6'),
    7: cssVar('--space-7'),
    8: cssVar('--space-8'),
  } satisfies Record<SpaceStep, TokenVar>,
  /** Typography tokens. GAP: bind to Blitzy OS on source arrival. */
  font: {
    family: cssVar('--font-family'),
    size: {
      xs: cssVar('--font-size-xs'),
      sm: cssVar('--font-size-sm'),
      md: cssVar('--font-size-md'),
      lg: cssVar('--font-size-lg'),
      xl: cssVar('--font-size-xl'),
    },
    weight: {
      regular: cssVar('--font-weight-regular'),
      semibold: cssVar('--font-weight-semibold'),
    },
    lineHeight: {
      tight: cssVar('--line-height-tight'),
      normal: cssVar('--line-height-normal'),
    },
  },
  /** Corner radii. GAP: bind to Blitzy OS on source arrival. */
  radius: {
    sm: cssVar('--radius-sm'),
    md: cssVar('--radius-md'),
  },
  /** Elevation shadows. GAP: bind to Blitzy OS on source arrival. */
  shadow: {
    1: cssVar('--shadow-1'),
    2: cssVar('--shadow-2'),
  },
  /** Stacking layers; tooltips sit above dialogs. GAP: bind to Blitzy OS on source arrival. */
  z: {
    banner: cssVar('--z-banner'),
    dialog: cssVar('--z-dialog'),
    tooltip: cssVar('--z-tooltip'),
  },
  /** Inactive-control and backdrop opacities. GAP: bind to Blitzy OS on source arrival. */
  opacity: {
    disabled: cssVar('--opacity-disabled'),
    backdrop: cssVar('--opacity-backdrop'),
  },
  /**
   * The full extent of the containing block, and the full inline size of the nearest inline-size
   * query container. GAP: bind to Blitzy OS on source arrival.
   */
  size: {
    full: cssVar('--size-full'),
    containerFull: cssVar('--size-container-full'),
  },
} as const;
