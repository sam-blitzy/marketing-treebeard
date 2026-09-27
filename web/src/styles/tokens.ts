/**
 * Design tokens: the single source of every design value in the web app.
 *
 * Request Rule 15 permits only design-system tokens and components. Every
 * `var(--...)` in authored CSS, every chart colour and every raw design value
 * used from TypeScript resolves to an entry of {@link tokenVars}. No other
 * file may hold a literal hex, rgba, px or duration value: web/.eslintrc.cjs
 * bans them everywhere in `src/` except here, so other modules (chart-theme.ts,
 * components, tests) derive their values from the exports below.
 *
 * `src/styles/tokens.css` is generated from {@link tokenVars} by
 * `npm run tokens` (scripts/generate-tokens.mjs) and drift-checked in CI by
 * `npm run tokens:check`. Edit this file and regenerate; never edit the CSS.
 *
 * Generator contract: the script transpiles this file with
 * `ts.transpileModule` and imports the output through a base64 `data:` URL,
 * which cannot resolve module specifiers. This file therefore imports nothing
 * (not even types), uses only erasable TypeScript syntax (annotations, type
 * aliases, interfaces, `as const`, `satisfies`, `as`), and does nothing at
 * module scope beyond building frozen constants from {@link tokenVars}.
 */

/**
 * Every design token of the Blitzy OS design system as an ordered, flat map of
 * CSS custom-property name to CSS value.
 *
 * Insertion order is the declaration order of the generated tokens.css
 * (165 custom properties). Names match `^--[a-z0-9-]+$`; values are single-line
 * CSS values without `;`, `{` or `}`. `--font-inter` is deliberately absent: it
 * is defined by next/font in src/app/fonts.ts on `<html>` (the `:root` element),
 * which is why the `var(--font-inter)` family values below resolve.
 *
 * Source: Figma file `oNL44XClMLFnF3jfbszzAG` ("Blitzy OS Design System"),
 * boards Colors 5964:7865 (Primitive tag 5964:7873), Typography 5964:7577,
 * Spacing 5964:11258, Atoms 4088:15122 and New comp (Alex) 7836:4157, as
 * reconciled in the plan's token manifest. Component-level tokens (marked
 * below) come from the Atoms and New comp components that use them.
 *
 * Design-system follow-ups recorded here so the values are not "corrected":
 * - Swatch 5964:7905 renders #005335 but is labelled a second "200"; it is
 *   `--color-aquamarine-300`. Swatch 5964:7916 renders #EC3636 but its label
 *   names a different hex; it is `--color-red-100`. In both cases the rendered
 *   value wins, and both label defects are flagged to the design-system owner.
 * - `--motion-spin-duration` and `--motion-spin-easing` are gap G10
 *   placeholders (see their entries).
 *
 * Excluded by design: gradients (none exist), dark-mode variants
 * (`Alternate=True`), Heading/Mobile styles (no mobile screens), the board
 * title typeface and all board chrome (dashed component-set borders and
 * swatch radii).
 */
export const tokenVars = {
  // Colour: primitive palette, Colors board 5964:7865 / Primitive 5964:7873.
  "--color-base-white": "#FFFFFF",
  "--color-base-black": "#000000",
  "--color-neutral-50": "#F5F5F5",
  "--color-neutral-100": "#D9D9D9",
  "--color-neutral-200": "#999999",
  "--color-neutral-300": "#666666",
  "--color-neutral-400": "#333333",
  "--color-neutral-500": "#0F0F11",
  "--color-primary-50": "#F2F0FE",
  "--color-primary-100": "#D4CBFC",
  "--color-primary-200": "#866BFD",
  /** Brand colour. */
  "--color-primary-300": "#5B39F3",
  "--color-primary-400": "#2D1C77",
  "--color-primary-500": "#1D1445",
  "--color-aquamarine-50": "#EAFEF7",
  "--color-aquamarine-100": "#C9FCEA",
  "--color-aquamarine-200": "#94FAD5",
  /** Swatch 5964:7905, mislabelled a second "200" in Figma; rendered value wins. */
  "--color-aquamarine-300": "#005335",
  "--color-purple-50": "#F5E2FF",
  "--color-purple-100": "#D89CF8",
  "--color-purple-200": "#B23AF2",
  "--color-red-50": "#FFDFDF",
  /** Swatch 5964:7916; its Figma label names another hex, the rendered value wins. */
  "--color-red-100": "#EC3636",
  "--color-red-200": "#991010",
  // Colour: component-level, Atoms 4088:15122 and New comp 7836:4157.
  "--color-border-subtle": "#E9E9E9",
  "--color-row-selected": "#F9F8FF",
  "--color-warning-bg": "#FFFBEA",
  "--color-progress-fill": "#00B473",
  "--color-scrim": "rgba(0,0,0,0.2)",
  "--color-divider": "rgba(0,0,0,0.15)",
  "--color-hover-overlay": "rgba(0,0,0,0.05)",
  "--color-chip-active": "rgba(212,203,252,0.5)",

  // Spacing: Spacing board 5964:11258.
  "--space-0-5": "2px",
  "--space-1": "4px",
  "--space-2": "8px",
  "--space-3": "12px",
  "--space-4": "16px",
  "--space-6": "24px",
  /** Component-derived (FilterContainer 8477:5327, table-action-bar 8338:4454); absent from the board. */
  "--space-8": "32px",
  "--space-12": "48px",
  "--space-16": "64px",
  // Spacing: component-level, PillToggle 4912:18045 and PickerDialog search input 8526:7430.
  "--space-pill-gap": "6px",
  "--space-input-icon-inset-start": "44px",
  "--space-input-icon-inset-end": "40px",

  // Typography: Typography board 5964:7577 (Heading/Desktop, Body/Desktop,
  // Body/Mobile Small, Button, inline code). Each style expands to family,
  // weight, size, line height and letter spacing.
  "--text-h1-family": "var(--font-inter)",
  "--text-h1-weight": "600",
  "--text-h1-size": "60px",
  /** Figma line height "auto", expressed as CSS `normal`. */
  "--text-h1-line-height": "normal",
  "--text-h1-letter-spacing": "-0.0083em",
  "--text-h2-family": "var(--font-inter)",
  "--text-h2-weight": "600",
  "--text-h2-size": "48px",
  "--text-h2-line-height": "1.2",
  "--text-h2-letter-spacing": "-0.0104em",
  "--text-h3-family": "var(--font-inter)",
  "--text-h3-weight": "600",
  "--text-h3-size": "40px",
  "--text-h3-line-height": "1.2",
  "--text-h3-letter-spacing": "0",
  "--text-h4-family": "var(--font-inter)",
  "--text-h4-weight": "600",
  "--text-h4-size": "32px",
  "--text-h4-line-height": "1.3",
  "--text-h4-letter-spacing": "0",
  "--text-h5-family": "var(--font-inter)",
  "--text-h5-weight": "600",
  "--text-h5-size": "24px",
  "--text-h5-line-height": "1.3",
  "--text-h5-letter-spacing": "0",
  "--text-h6-family": "var(--font-inter)",
  "--text-h6-weight": "600",
  "--text-h6-size": "20px",
  "--text-h6-line-height": "1.3",
  "--text-h6-letter-spacing": "0",
  "--text-body-family": "var(--font-inter)",
  "--text-body-weight": "400",
  "--text-body-size": "16px",
  "--text-body-line-height": "1.5",
  "--text-body-letter-spacing": "-0.0188em",
  "--text-body-bold-family": "var(--font-inter)",
  "--text-body-bold-weight": "600",
  "--text-body-bold-size": "16px",
  "--text-body-bold-line-height": "1.5",
  "--text-body-bold-letter-spacing": "-0.0025em",
  "--text-small-family": "var(--font-inter)",
  "--text-small-weight": "400",
  "--text-small-size": "14px",
  "--text-small-line-height": "1.5",
  "--text-small-letter-spacing": "0",
  "--text-small-bold-family": "var(--font-inter)",
  "--text-small-bold-weight": "600",
  "--text-small-bold-size": "14px",
  "--text-small-bold-line-height": "1.5",
  "--text-small-bold-letter-spacing": "0",
  /** Body/Desktop/Subtle; consumers pair it with `--color-neutral-200`. */
  "--text-subtle-family": "var(--font-inter)",
  "--text-subtle-weight": "400",
  "--text-subtle-size": "12px",
  "--text-subtle-line-height": "1.4",
  "--text-subtle-letter-spacing": "-0.003em",
  "--text-compact-family": "var(--font-inter)",
  "--text-compact-weight": "400",
  "--text-compact-size": "14px",
  "--text-compact-line-height": "1.4",
  "--text-compact-letter-spacing": "0",
  "--text-compact-bold-family": "var(--font-inter)",
  "--text-compact-bold-weight": "600",
  "--text-compact-bold-size": "14px",
  "--text-compact-bold-line-height": "1.4",
  "--text-compact-bold-letter-spacing": "0",
  "--text-button-sm-family": "var(--font-inter)",
  "--text-button-sm-weight": "600",
  "--text-button-sm-size": "16px",
  "--text-button-sm-line-height": "24px",
  "--text-button-sm-letter-spacing": "0",
  "--text-button-lg-family": "var(--font-inter)",
  "--text-button-lg-weight": "600",
  "--text-button-lg-size": "18px",
  "--text-button-lg-line-height": "28px",
  "--text-button-lg-letter-spacing": "0",
  /** Menlo is not a web font, so the family carries a monospace fallback stack. */
  "--text-code-family": 'Menlo, Consolas, "Liberation Mono", monospace',
  "--text-code-weight": "400",
  "--text-code-size": "14px",
  "--text-code-line-height": "20px",
  "--text-code-letter-spacing": "0",

  // Radius.
  "--radius-2": "2px",
  "--radius-4": "4px",
  "--radius-6": "6px",
  "--radius-8": "8px",
  "--radius-12": "12px",
  "--radius-16": "16px",
  "--radius-24": "24px",
  "--radius-32": "32px",
  /** Full-round pill (Select 4088:15873, toggle); one token for both Figma values. */
  "--radius-full": "9999px",

  // Border widths.
  "--border-hairline": "0.5px",
  "--border-1": "1px",
  "--border-2": "2px",
  /** Focus ring and tab underline (TextInput focus 4088:15773, Tab Nav 4088:15421). */
  "--border-focus": "4px",

  // Sizes: confirmed component dimensions only.
  "--size-icon-16": "16px",
  "--size-icon-20": "20px",
  "--size-icon-24": "24px",
  "--size-control-sm": "40px",
  "--size-control-lg": "48px",
  "--size-tag": "24px",
  "--size-nav-width": "276px",
  "--size-drawer-width": "480px",
  "--size-dialog-width": "560px",
  "--size-field-width": "560px",
  "--size-textarea-height": "180px",
  "--size-popover-width": "432px",
  "--size-tooltip-width": "240px",
  "--size-toast-width": "431px",
  "--size-card-min": "448px",
  "--size-progress-width": "148px",
  "--size-progress-height": "6px",
  "--size-progress-tick-width": "2px",
  "--size-progress-tick-height": "9px",
  "--size-table-input-width": "72px",
  "--size-table-input-height": "32px",

  // Motion.
  /**
   * Gap G10 placeholder: loader-alt 5964:8680 specifies continuous rotation
   * with no duration. Flagged to the design-system team for a confirmed value.
   */
  "--motion-spin-duration": "1s",
  /**
   * Gap G10 placeholder: loader-alt 5964:8680 specifies no easing. Flagged to
   * the design-system team for a confirmed value.
   */
  "--motion-spin-easing": "linear",

  // Opacity.
  /** Primary Disabled button (Button 4088:15126). */
  "--opacity-disabled": "0.5",

  // Shadows.
  "--shadow-xs": "0 1px 2px 0 rgba(0,0,0,0.06), 0 1px 3px 0 rgba(0,0,0,0.1)",
  "--shadow-sm": "0 2px 4px -2px rgba(0,0,0,0.06), 1px 3px 12px 3px rgba(0,0,0,0.1)",
  "--shadow-md": "0 4px 6px -2px rgba(0,0,0,0.03), 0 12px 16px -4px rgba(0,0,0,0.08)",
  /** Figma effect_25ef099f (Environment set switcher dialog 8526:7430). */
  "--shadow-dialog":
    "0 -9px 50px 0 rgba(0,0,0,0.06), 0 374px 105px 0 rgba(130,130,130,0), 0 239px 96px 0 rgba(130,130,130,0.01), 0 135px 81px 0 rgba(130,130,130,0.05), 0 60px 60px 0 rgba(130,130,130,0.09), 0 15px 33px 0 rgba(130,130,130,0.1)",
} as const;

/** Name of a design token: a key of {@link tokenVars}, such as `"--space-4"`. */
export type TokenName = keyof typeof tokenVars;

/**
 * CSS reference to a token, for handing a token to CSS from TypeScript (for
 * example as the value of a custom property).
 *
 * @param name - Token to reference.
 * @returns `var(<name>)`, e.g. `cssVar("--color-primary-300")` is
 *   `"var(--color-primary-300)"`.
 */
export function cssVar(name: TokenName): string {
  return `var(${name})`;
}

/** A px length (`12px`, `0.5px`) or a unitless number (`600`, `0.5`). */
const PX_OR_UNITLESS = /^(-?\d+(?:\.\d+)?)(?:px)?$/;

/**
 * Numeric value of a px-valued (or unitless numeric) token, for APIs that take
 * numbers rather than CSS strings, such as Recharts SVG props.
 *
 * @param name - Token whose value is a px length or a unitless number.
 * @returns The number, e.g. `tokenPx("--space-3")` is `12` and
 *   `tokenPx("--border-hairline")` is `0.5`.
 * @throws Error when the token's value is not a px length or unitless number
 *   (for example an em letter spacing, a duration, a colour or a shadow).
 */
export function tokenPx(name: TokenName): number {
  const value: string = tokenVars[name];
  const digits = PX_OR_UNITLESS.exec(value)?.[1];
  if (digits === undefined) {
    throw new Error(
      `tokenPx: token ${name} has value "${value}", which is not a px length or unitless number`,
    );
  }
  return Number(digits);
}

// Grouped raw-value accessors.

/** Token-name prefixes that form a group accessor below. */
type TokenPrefix =
  | "--color-"
  | "--space-"
  | "--radius-"
  | "--border-"
  | "--size-"
  | "--shadow-"
  | "--motion-"
  | "--opacity-";

/**
 * Suffixes of the token names that start with prefix `P`. The conditional
 * distributes over each name, so `--color-border-subtle` is a `--color-` suffix
 * and never a `--border-` one.
 */
type SuffixOf<P extends TokenPrefix, N extends TokenName = TokenName> = N extends `${P}${infer S}`
  ? S
  : never;

/** Read-only map from token-name suffix to the token's literal value. */
type TokenGroup<P extends TokenPrefix> = {
  readonly [S in SuffixOf<P>]: (typeof tokenVars)[`${P}${S}` & TokenName];
};

/**
 * Collects the tokens whose names start with `prefix`, keyed by the rest of the
 * name. The result is frozen. Its keys enumerate in JavaScript property order,
 * so integer-like suffixes (`"1"`, `"4"`) come first; iterate
 * {@link tokenVars} when declaration order matters.
 */
function tokenGroup<P extends TokenPrefix>(prefix: P): TokenGroup<P> {
  const members: [string, string][] = [];
  for (const [name, value] of Object.entries(tokenVars)) {
    if (name.startsWith(prefix)) {
      members.push([name.slice(prefix.length), value]);
    }
  }
  return Object.freeze(Object.fromEntries(members)) as TokenGroup<P>;
}

/**
 * Colour tokens by name suffix, e.g. `color["primary-300"]` is `"#5B39F3"` and
 * `color["border-subtle"]` is `"#E9E9E9"`. Use raw values only where CSS
 * variables cannot reach (SVG presentation attributes); CSS uses `var()`.
 */
export const color = tokenGroup("--color-");

/** Spacing tokens by name suffix, e.g. `space["4"]` is `"16px"` and `space["0-5"]` is `"2px"`. */
export const space = tokenGroup("--space-");

/** Corner-radius tokens by name suffix, e.g. `radius["4"]` is `"4px"` and `radius.full` is `"9999px"`. */
export const radius = tokenGroup("--radius-");

/** Border-width tokens by name suffix, e.g. `border["1"]` is `"1px"` and `border.focus` is `"4px"`. */
export const border = tokenGroup("--border-");

/** Component size tokens by name suffix, e.g. `size["icon-24"]` is `"24px"`. */
export const size = tokenGroup("--size-");

/** Box-shadow tokens by name suffix: `xs`, `sm`, `md` and `dialog`. */
export const shadow = tokenGroup("--shadow-");

/**
 * Motion tokens by name suffix: `spin-duration` and `spin-easing`, both gap G10
 * placeholders pending confirmed values from the design-system team.
 */
export const motion = tokenGroup("--motion-");

/** Opacity tokens by name suffix, e.g. `opacity.disabled` is `"0.5"`. */
export const opacity = tokenGroup("--opacity-");

// Typography.

/**
 * Name of a typography style, derived from the `--text-<style>-family` tokens:
 * `"h1" | "h2" | ... | "button-lg" | "code"`.
 */
export type TypographyStyle =
  TokenName extends infer N ? (N extends `--text-${infer S}-family` ? S : never) : never;

/**
 * The resolved values of one typography style. With the default type argument
 * it is the general shape; with a specific style it keeps that style's literal
 * values.
 */
export interface TextStyle<S extends TypographyStyle = TypographyStyle> {
  /** `font-family` value. */
  readonly family: (typeof tokenVars)[`--text-${S}-family`];
  /** `font-weight` value. */
  readonly weight: (typeof tokenVars)[`--text-${S}-weight`];
  /** `font-size` value (px). */
  readonly size: (typeof tokenVars)[`--text-${S}-size`];
  /** `line-height` value (unitless, px or `normal`). */
  readonly lineHeight: (typeof tokenVars)[`--text-${S}-line-height`];
  /** `letter-spacing` value (em or `0`). */
  readonly letterSpacing: (typeof tokenVars)[`--text-${S}-letter-spacing`];
}

/** Every typography style, each with its own literal values. */
export type TextTokens = { readonly [S in TypographyStyle]: TextStyle<S> };

/** Resolves to `T` only when `T` names every typography style, otherwise to `never`. */
type CoversEveryStyle<T extends readonly TypographyStyle[]> = [
  Exclude<TypographyStyle, T[number]>,
] extends [never]
  ? T
  : never;

/** The 16 typography styles in design-system order (headings, body, buttons, code). */
export const typographyStyles = [
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "body",
  "body-bold",
  "small",
  "small-bold",
  "subtle",
  "compact",
  "compact-bold",
  "button-sm",
  "button-lg",
  "code",
] as const satisfies readonly TypographyStyle[];

/** Reads the five `--text-<style>-*` tokens of one style. */
function textStyle<S extends TypographyStyle>(style: S): TextStyle<S> {
  return Object.freeze({
    family: tokenVars[`--text-${style}-family`],
    weight: tokenVars[`--text-${style}-weight`],
    size: tokenVars[`--text-${style}-size`],
    lineHeight: tokenVars[`--text-${style}-line-height`],
    letterSpacing: tokenVars[`--text-${style}-letter-spacing`],
  });
}

/**
 * Builds {@link text} from the style list. The parameter type rejects, at
 * compile time, a list that omits any style defined in {@link tokenVars}.
 */
function buildText(styles: CoversEveryStyle<typeof typographyStyles>): TextTokens {
  return Object.freeze(
    Object.fromEntries(styles.map((style) => [style, textStyle(style)])),
  ) as TextTokens;
}

/**
 * Typography tokens by style, e.g. `text.subtle.size` is `"12px"` and
 * `text.body.family` is `"var(--font-inter)"`. CSS uses the
 * `--text-<style>-*` custom properties instead.
 */
export const text = buildText(typographyStyles);

// Charts.

/**
 * Chart series colours in series order: series 1 is Google Ads, series 2 is
 * LinkedIn, and per-campaign series (at most 5) take them in selection order.
 * Each entry is a colour token value; chart-theme.ts builds on this palette.
 */
export const chartPalette = [
  color["primary-300"],
  color["purple-200"],
  color["aquamarine-300"],
  color["primary-200"],
  color["primary-400"],
] as const;

