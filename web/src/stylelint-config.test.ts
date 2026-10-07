// @vitest-environment node
/**
 * Behaviour tests for web/.stylelintrc.json.
 *
 * Each rule is exercised through stylelint's Node API on virtual stylesheets: `stylelint.lint`
 * takes the code and a path relative to web/, and stylelint resolves the real configuration and
 * its overrides for that path, so these tests see exactly what `npm run lint` sees. Every case
 * asserts the complete list of warnings (rule, severity, line and message), which proves both
 * that a restriction fires where it must and that nothing else fires where the config allows
 * the construct. The committed design-system stylesheets are linted from disk as well, so a rule
 * change that would reject them fails here.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import stylelint from 'stylelint';
import { beforeAll, describe, expect, it } from 'vitest';

/** The web/ package root, which holds .stylelintrc.json. Derived from this file's URL. */
const WEB_URL = new URL('..', import.meta.url);
const WEB_DIR = fileURLToPath(WEB_URL);

const FEATURE_CSS = 'src/features/actions/Example.css';
const FEATURE_MODULE_CSS = 'src/features/actions/sections/Example.module.css';
const COMPONENT_CSS = 'src/design-system/components/Example.css';
const TOKENS_CSS = 'src/design-system/tokens.css';

/** The only component stylesheets that may hold the documented 48rem Grid breakpoint. */
const BREAKPOINT_STYLESHEETS = [
  'src/design-system/components/Grid.css',
  'src/design-system/components/PageShell.css',
] as const;

/** The stylesheets committed under src/design-system, which must lint clean as they are. */
const COMMITTED_STYLESHEETS = [
  TOKENS_CSS,
  'src/design-system/components/Banner.css',
  'src/design-system/components/Card.css',
  'src/design-system/components/EmptyState.css',
  'src/design-system/components/Grid.css',
  'src/design-system/components/Inline.css',
  'src/design-system/components/Stack.css',
  'src/design-system/components/Tabs.css',
  'src/design-system/components/TextField.css',
  'src/design-system/components/shared.css',
] as const;

/** One stylelint warning, reduced to the fields these tests assert. */
interface Report {
  readonly rule: string;
  readonly severity: string;
  readonly line: number;
  readonly text: string;
}

/**
 * Lints `codeLines` (joined with newlines) as the virtual stylesheet `filePath`, relative to
 * web/, and returns its warnings ordered by line, column and rule name.
 *
 * stylelint pushes warnings in rule execution order, which is not part of its contract, so the
 * position sort keeps the expected lists deterministic. An ignored path, an invalid rule option,
 * a deprecation or a parse error throws, so an "allowed" case can never pass because the file
 * fell outside the config or a rule silently switched itself off.
 */
async function lint(filePath: string, ...codeLines: readonly string[]): Promise<Report[]> {
  const { results } = await stylelint.lint({
    code: codeLines.join('\n'),
    codeFilename: filePath,
    cwd: WEB_DIR,
  });
  const [result] = results;
  if (result === undefined || results.length !== 1) {
    throw new Error(`stylelint returned ${String(results.length)} results for ${filePath}`);
  }
  if (result.ignored === true) {
    throw new Error(`stylelint ignored ${filePath}`);
  }
  const problems = [
    ...result.invalidOptionWarnings.map(({ text }) => text),
    ...result.deprecations.map(({ text }) => text),
    ...result.parseErrors.map(({ text }) => text),
  ];
  if (problems.length > 0) {
    throw new Error(`stylelint could not apply the config to ${filePath}: ${problems.join('; ')}`);
  }
  return [...result.warnings]
    .sort((a, b) => a.line - b.line || a.column - b.column || a.rule.localeCompare(b.rule))
    .map(({ rule, severity, line, text }) => ({ rule, severity, line, text }));
}

/** The error `rule` reports on `line` with the custom `message`, which stylelint suffixes. */
function report(rule: string, line: number, message: string): Report {
  return { rule, severity: 'error', line, text: `${message} (${rule})` };
}

/** Feature CSS: `value` of `property` is not a token or one of the six literal exceptions. */
function featureValue(line: number, property: string, value: string): Report {
  return report(
    'declaration-property-value-allowed-list',
    line,
    `Property "${property}" has disallowed value "${value}". Feature CSS uses only design tokens: var(--token) with no fallback, or 0, none, auto, inherit, currentColor, transparent`,
  );
}

/** Component CSS: `value` of a token-backed `property` is not a token-only value. */
function componentValue(line: number, property: string, value: string): Report {
  return report(
    'declaration-property-value-allowed-list',
    line,
    `Property "${property}" has disallowed value "${value}". Colour, spacing, typography (the font shorthand included), radius, box and text shadow and z-index values are var(--token) with no fallback, or 0, none, auto, inherit, currentColor, transparent. Positional insets (inset, inset-*, top, right, bottom, left) take those values, a percentage, or calc() over var(--token), unitless numbers and percentages`,
  );
}

/** Feature and component CSS: a hex colour literal. */
function hexColor(line: number, color: string): Report {
  return report(
    'color-no-hex',
    line,
    `Disallowed hex color "${color}". Use a colour token from src/design-system/tokens.css`,
  );
}

/** Feature and component CSS: a named colour literal. */
function namedColor(line: number, color: string): Report {
  return report(
    'color-named',
    line,
    `Disallowed named color "${color}". Use a colour token from src/design-system/tokens.css`,
  );
}

/** Component CSS: a colour function in place of a colour token. */
function colourFunction(line: number, name: string): Report {
  return report(
    'function-disallowed-list',
    line,
    `Disallowed colour function "${name}". Colours come only from tokens in src/design-system/tokens.css`,
  );
}

/** Feature CSS: a layout property that only the Stack, Inline and Grid primitives may set. */
function layoutProperty(line: number, property: string): Report {
  return report(
    'property-disallowed-list',
    line,
    `Disallowed property "${property}". Feature code lays out only through the Stack, Inline and Grid primitives from src/design-system`,
  );
}

/** Anywhere but tokens.css: an `!important` declaration. */
function important(line: number): Report {
  return report(
    'declaration-no-important',
    line,
    'Disallowed !important. Only src/design-system/tokens.css may use it, for its prefers-reduced-motion override (WCAG 2.3.3)',
  );
}

/** Feature CSS: a media feature other than the keyword user preferences. */
function featureMediaName(line: number, name: string): Report {
  return report(
    'media-feature-name-allowed-list',
    line,
    `Disallowed media feature "${name}". Feature CSS has no breakpoints: responsive layout comes only from the Stack, Inline and Grid primitives in src/design-system. A feature media query may test only the keyword user preferences prefers-reduced-motion, prefers-reduced-transparency, prefers-contrast, prefers-color-scheme, forced-colors and inverted-colors`,
  );
}

/** Feature CSS: a user-preference media feature tested against a non-keyword value. */
function featureMediaValue(line: number, name: string, value: string): Report {
  return report(
    'media-feature-name-value-allowed-list',
    line,
    `Media feature "${name}" has disallowed value "${value}". A user-preference query in feature CSS takes only that feature's keyword values`,
  );
}

/** Feature CSS: an at-rule that can carry a breakpoint. */
function featureAtRule(line: number, atRule: string): Report {
  return report(
    'at-rule-disallowed-list',
    line,
    `Disallowed at-rule "${atRule}". Feature CSS has no container queries, custom media or @import, because each can carry a breakpoint: responsive layout comes only from the Stack, Inline and Grid primitives in src/design-system`,
  );
}

/** Feature CSS: a reference to a custom media query. */
function featureCustomMedia(line: number, name: string): Report {
  return report(
    'no-unknown-custom-media',
    line,
    `Unknown custom media query "${name}". Feature CSS has no breakpoints, so it defines and references no custom media: responsive layout comes only from the Stack, Inline and Grid primitives in src/design-system`,
  );
}

/**
 * Component CSS other than Grid.css and PageShell.css: a dimensional (width, height,
 * inline-size, block-size), viewport-shape (aspect-ratio) or device-size feature.
 */
function componentMediaName(line: number, name: string): Report {
  return report(
    'media-feature-name-disallowed-list',
    line,
    `Disallowed media feature "${name}". Component CSS has no breakpoints of its own: responsive layout comes from the Grid primitive and PageShell, whose Grid.css and PageShell.css alone hold the documented 48rem Grid breakpoint. Width, height, inline-size, block-size, aspect-ratio and device-width/height conditions are disallowed here; keyword preference queries such as prefers-reduced-motion stay allowed`,
  );
}

/** Grid.css and PageShell.css: a viewport-shape (aspect-ratio) or device-size feature. */
function breakpointMediaName(line: number, name: string): Report {
  return report(
    'media-feature-name-disallowed-list',
    line,
    `Disallowed media feature "${name}". Grid.css and PageShell.css break only at the documented 48rem Grid breakpoint, a width or height condition; viewport-shape (aspect-ratio) and device-size (device-width, device-height) queries carry other breakpoints`,
  );
}

/** Grid.css and PageShell.css: a width or height condition other than 48rem. */
function breakpointMediaValue(line: number, name: string, value: string): Report {
  return report(
    'media-feature-name-value-allowed-list',
    line,
    `Media feature "${name}" has disallowed value "${value}". Grid.css and PageShell.css hold the one documented Grid breakpoint, so every width and height condition in them is exactly 48rem`,
  );
}

/** Component CSS: an at-rule that can carry a breakpoint other than 48rem. */
function componentAtRule(line: number, atRule: string): Report {
  return report(
    'at-rule-disallowed-list',
    line,
    `Disallowed at-rule "${atRule}". Component CSS has no container queries or custom media, because each can carry a breakpoint other than the 48rem one that Grid.css documents`,
  );
}

/** Component CSS: a reference to a custom media query. */
function componentCustomMedia(line: number, name: string): Report {
  return report(
    'no-unknown-custom-media',
    line,
    `Unknown custom media query "${name}". Component CSS defines no custom media, so the documented 48rem Grid breakpoint is written out, in Grid.css and PageShell.css only`,
  );
}

/** A one-rule stylesheet whose rule holds the single declaration `declaration`, on line 2. */
function ruleWith(declaration: string): readonly string[] {
  return ['.example {', `  ${declaration}`, '}'];
}

/** A stylesheet whose first line opens the block at-rule `prelude` around a token-only rule. */
function blockAtRule(prelude: string): readonly string[] {
  return [`${prelude} {`, '  .example {', '    padding: var(--space-2);', '  }', '}'];
}

beforeAll(async () => {
  // Loading stylelint's rules and resolving the config takes a moment on a busy host; do it
  // once, outside the per-test timeout.
  await lint(COMPONENT_CSS, '.warm-up {', '  color: var(--color-text);', '}');
}, 120_000);

describe('component token values (AAP 0.4.2, AAP 0.4.4)', () => {
  it.each([
    ['font', '600 12px/1.2 Arial'],
    ['font', 'var(--font-weight-semibold) var(--font-size-sm)/var(--line-height-tight) Arial'],
    ['text-shadow', '1px 2px var(--color-text)'],
    ['inset-block-start', '9px'],
    ['inset-inline', '1rem auto'],
    ['inset', '4px'],
    ['top', '3px'],
    ['right', '2em'],
    ['left', 'calc(100% + 4px)'],
    ['bottom', 'min(100%, var(--space-2))'],
    ['inset-block-start', 'var(--space-2, 8px)'],
    ['font-size', '12px'],
    ['padding', '8px'],
    ['PADDING', '8px'],
    ['Font-Size', '12px'],
    ['TOP', '3px'],
    ['TEXT-SHADOW', '1px 1px var(--color-text)'],
    ['FONT', '600 12px/1.2 Arial'],
  ])('reports %s: %s', async (property, value) => {
    expect(await lint(COMPONENT_CSS, ...ruleWith(`${property}: ${value};`))).toEqual([
      componentValue(2, property, value),
    ]);
  });

  it.each([
    'font: inherit;',
    'font: var(--font-size-md);',
    'text-shadow: none;',
    'text-shadow: var(--shadow-1);',
    'inset: 0;',
    'inset: 0 auto auto 0;',
    'inset-block-start: var(--space-2);',
    'inset-block-start: calc(100% + var(--space-1));',
    'inset-inline-start: 0;',
    'top: 50%;',
    'left: calc(var(--space-2) * -1);',
    'bottom: calc((100% - var(--space-4)) / 2);',
    'PADDING: var(--space-2);',
    'FONT: inherit;',
  ])('allows %s', async (declaration) => {
    expect(await lint(COMPONENT_CSS, ...ruleWith(declaration))).toEqual([]);
  });

  it.each([
    ['-webkit-box-shadow', '0 1px 2px var(--color-border)'],
    ['-webkit-border-radius', '4px'],
    ['-webkit-margin-start', '8px'],
    ['-WEBKIT-BOX-SHADOW', '0 1px 2px var(--color-border)'],
    ['-moz-border-radius-topleft', '4px'],
    ['-ms-padding-start', '8px'],
    ['-o-border-top-left-radius', '4px'],
  ])('reports the vendor-prefixed %s: %s', async (property, value) => {
    expect(await lint(COMPONENT_CSS, ...ruleWith(`${property}: ${value};`))).toEqual([
      componentValue(2, property, value),
    ]);
  });

  it.each([
    '-webkit-box-shadow: var(--shadow-1);',
    '-moz-border-radius-topleft: var(--radius-sm);',
    '-webkit-font-smoothing: antialiased;',
    '-webkit-appearance: none;',
  ])('allows the vendor-prefixed %s', async (declaration) => {
    expect(await lint(COMPONENT_CSS, ...ruleWith(declaration))).toEqual([]);
  });

  it('reports a named colour', async () => {
    expect(await lint(COMPONENT_CSS, ...ruleWith('color: red;'))).toEqual([
      namedColor(2, 'red'),
      componentValue(2, 'color', 'red'),
    ]);
  });

  it('reports a named colour on a mixed-case colour property', async () => {
    expect(await lint(COMPONENT_CSS, ...ruleWith('Border-Top-Color: red;'))).toEqual([
      namedColor(2, 'red'),
      componentValue(2, 'Border-Top-Color', 'red'),
    ]);
  });

  it('reports a literal box shadow and its colour function', async () => {
    expect(await lint(COMPONENT_CSS, ...ruleWith('box-shadow: 0 1px 2px rgba(0,0,0,.2);'))).toEqual(
      [componentValue(2, 'box-shadow', '0 1px 2px rgba(0,0,0,.2)'), colourFunction(2, 'rgba')],
    );
  });

  it('allows layout keywords, token gaps and track calculations', async () => {
    expect(
      await lint(
        COMPONENT_CSS,
        '.example {',
        '  display: flex;',
        '  gap: var(--space-2);',
        '  position: absolute;',
        '  text-align: end;',
        '  font-variant-numeric: tabular-nums;',
        '  grid-template-columns: repeat(auto-fit, minmax(min(100%, calc(var(--space-8) * 6)), 1fr));',
        '}',
      ),
    ).toEqual([]);
  });

  it('reports !important outside tokens.css', async () => {
    expect(await lint(COMPONENT_CSS, ...ruleWith('color: var(--color-text) !important;'))).toEqual(
      [important(2)],
    );
  });
});

describe('feature token values and layout (AAP 0.4.4)', () => {
  it.each([FEATURE_CSS, FEATURE_MODULE_CSS])('reports literal values in %s', async (filePath) => {
    expect(
      await lint(
        filePath,
        '.example {',
        '  padding: 8px;',
        '  color: #333;',
        '  margin: var(--space-2, 8px);',
        '}',
      ),
    ).toEqual([
      featureValue(2, 'padding', '8px'),
      hexColor(3, '#333'),
      featureValue(3, 'color', '#333'),
      featureValue(4, 'margin', 'var(--space-2, 8px)'),
    ]);
  });

  it('reports custom layout', async () => {
    expect(await lint(FEATURE_CSS, ...ruleWith('display: flex;'))).toEqual([
      layoutProperty(2, 'display'),
      featureValue(2, 'display', 'flex'),
    ]);
  });

  it('allows tokens and the literal exceptions', async () => {
    expect(
      await lint(
        FEATURE_CSS,
        '.example {',
        '  padding: var(--space-2) var(--space-4);',
        '  border: none;',
        '  color: currentColor;',
        '}',
      ),
    ).toEqual([]);
  });
});

describe('feature breakpoints (AAP 0.4.4, AAP 0.5.5)', () => {
  it.each([
    ['@media (min-width: 900px)', 'min-width'],
    ['@media screen and (width >= 600px)', 'width'],
    ['@media (600px <= width)', 'width'],
    ['@media (max-width: 48rem)', 'max-width'],
    ['@media (MIN-WIDTH: 900px)', 'MIN-WIDTH'],
    ['@media (MAX-WIDTH: 900px)', 'MAX-WIDTH'],
    ['@media (Min-Width: 900px)', 'Min-Width'],
    ['@media (orientation: landscape)', 'orientation'],
  ])('reports %s', async (prelude, name) => {
    expect(await lint(FEATURE_CSS, ...blockAtRule(prelude))).toEqual([featureMediaName(1, name)]);
  });

  it('reports a breakpoint in a CSS module and nested in a rule', async () => {
    expect(
      await lint(
        FEATURE_MODULE_CSS,
        '.example {',
        '  @media (min-width: 900px) {',
        '    padding: var(--space-2);',
        '  }',
        '}',
      ),
    ).toEqual([featureMediaName(2, 'min-width')]);
  });

  it.each([
    ['@media (prefers-reduced-motion: 900px)', 'prefers-reduced-motion', '900px'],
    ['@media (PREFERS-REDUCED-MOTION: 12px)', 'PREFERS-REDUCED-MOTION', '12px'],
    ['@media (Prefers-Color-Scheme: reduce)', 'Prefers-Color-Scheme', 'reduce'],
  ])('reports the non-keyword value in %s', async (prelude, name, value) => {
    expect(await lint(FEATURE_CSS, ...blockAtRule(prelude))).toEqual([
      featureMediaValue(1, name, value),
    ]);
  });

  it.each(['@container (min-width: 30em)', '@Container card (min-width: 30em)'])(
    'reports the container query %s',
    async (prelude) => {
      const atRule = prelude.slice(0, prelude.indexOf(' '));
      expect(await lint(FEATURE_CSS, ...blockAtRule(prelude))).toEqual([
        featureAtRule(1, atRule),
      ]);
    },
  );

  it.each([
    ['@custom-media --narrow (max-width: 30em);', '@custom-media'],
    ['@import url("./wide.css") (min-width: 900px);', '@import'],
  ])('reports %s', async (statement, atRule) => {
    expect(await lint(FEATURE_CSS, statement)).toEqual([featureAtRule(1, atRule)]);
  });

  it('reports a custom media reference', async () => {
    expect(await lint(FEATURE_CSS, ...blockAtRule('@media (--narrow)'))).toEqual([
      featureCustomMedia(1, '--narrow'),
    ]);
  });

  it.each([
    '@media (prefers-reduced-motion: reduce)',
    '@media (prefers-color-scheme: dark), (forced-colors: active), (prefers-contrast: more)',
    '@media print',
    '@media (Prefers-Reduced-Motion: reduce)',
    '@media (prefers-reduced-motion: REDUCE)',
    '@media (PREFERS-COLOR-SCHEME: Dark), (Forced-Colors: ACTIVE)',
  ])('allows %s', async (prelude) => {
    expect(await lint(FEATURE_CSS, ...blockAtRule(prelude))).toEqual([]);
  });
});

describe('component breakpoints (AAP 0.4.4, AAP 0.5.5)', () => {
  it.each([
    ['src/design-system/components/Inline.css', '@media (max-width: 48rem)', 'max-width'],
    ['src/design-system/components/Spinner.css', '@media (max-width: 48rem)', 'max-width'],
    [COMPONENT_CSS, '@media (max-width: 48rem)', 'max-width'],
    [COMPONENT_CSS, '@media screen and (max-width: 48rem)', 'max-width'],
    [COMPONENT_CSS, '@media (MAX-WIDTH: 48REM)', 'MAX-WIDTH'],
    [COMPONENT_CSS, '@media (min-height: 48rem)', 'min-height'],
    [COMPONENT_CSS, '@media (width < 48rem)', 'width'],
    [COMPONENT_CSS, '@media (40rem <= width)', 'width'],
    [COMPONENT_CSS, '@media (min-width: 900px)', 'min-width'],
    [COMPONENT_CSS, '@media (Min-Inline-Size: 40rem)', 'Min-Inline-Size'],
    [COMPONENT_CSS, '@media (max-block-size: 48rem)', 'max-block-size'],
    [COMPONENT_CSS, '@media (min-aspect-ratio: 16/9)', 'min-aspect-ratio'],
    [COMPONENT_CSS, '@media (aspect-ratio > 1)', 'aspect-ratio'],
    [COMPONENT_CSS, '@media (Device-Aspect-Ratio: 16/9)', 'Device-Aspect-Ratio'],
    [COMPONENT_CSS, '@media (max-device-width: 900px)', 'max-device-width'],
    [COMPONENT_CSS, '@media (MAX-DEVICE-WIDTH: 48rem)', 'MAX-DEVICE-WIDTH'],
    [COMPONENT_CSS, '@media (device-height: 48rem)', 'device-height'],
  ])('reports in %s the media feature of %s', async (filePath, prelude, name) => {
    expect(await lint(filePath, ...blockAtRule(prelude))).toEqual([componentMediaName(1, name)]);
  });

  it('reports a breakpoint nested in a rule', async () => {
    expect(
      await lint(
        COMPONENT_CSS,
        '.example {',
        '  @media (max-width: 48rem) {',
        '    padding: var(--space-2);',
        '  }',
        '}',
      ),
    ).toEqual([componentMediaName(2, 'max-width')]);
  });

  it('reports a container query', async () => {
    expect(await lint(COMPONENT_CSS, ...blockAtRule('@container (min-width: 48rem)'))).toEqual([
      componentAtRule(1, '@container'),
    ]);
  });

  it('reports a custom media definition', async () => {
    expect(await lint(COMPONENT_CSS, '@custom-media --narrow (max-width: 48rem);')).toEqual([
      componentAtRule(1, '@custom-media'),
    ]);
  });

  it('reports a custom media reference', async () => {
    expect(await lint(COMPONENT_CSS, ...blockAtRule('@media (--narrow)'))).toEqual([
      componentCustomMedia(1, '--narrow'),
    ]);
  });

  it.each([
    [COMPONENT_CSS, '@media (prefers-reduced-motion: reduce)'],
    ['src/design-system/components/Spinner.css', '@media (prefers-reduced-motion: reduce)'],
    [COMPONENT_CSS, '@media (Prefers-Reduced-Motion: reduce)'],
    [COMPONENT_CSS, '@media print'],
  ])('allows in %s %s', async (filePath, prelude) => {
    expect(await lint(filePath, ...blockAtRule(prelude))).toEqual([]);
  });
});

describe.each(BREAKPOINT_STYLESHEETS)('the Grid breakpoint in %s (AAP 0.5.5)', (filePath) => {
  it.each([
    ['@media (max-width: 40rem)', 'max-width', '40rem'],
    ['@media (min-width: 900px)', 'min-width', '900px'],
    ['@media (MAX-WIDTH: 40rem)', 'MAX-WIDTH', '40rem'],
    ['@media (min-height: 30em)', 'min-height', '30em'],
    ['@media (width < 40rem)', 'width', '40rem'],
    ['@media (40rem <= width < 48rem)', 'width', '40rem'],
    ['@media (Min-Inline-Size: 40rem)', 'Min-Inline-Size', '40rem'],
  ])('reports %s', async (prelude, name, value) => {
    expect(await lint(filePath, ...blockAtRule(prelude))).toEqual([
      breakpointMediaValue(1, name, value),
    ]);
  });

  it.each([
    ['@media (min-aspect-ratio: 16/9)', 'min-aspect-ratio'],
    ['@media (aspect-ratio > 1)', 'aspect-ratio'],
    ['@media (Device-Aspect-Ratio: 16/9)', 'Device-Aspect-Ratio'],
    ['@media (max-device-width: 900px)', 'max-device-width'],
    ['@media (MAX-DEVICE-WIDTH: 48rem)', 'MAX-DEVICE-WIDTH'],
    ['@media (device-height: 48rem)', 'device-height'],
  ])('reports the viewport-shape or device-size feature in %s', async (prelude, name) => {
    expect(await lint(filePath, ...blockAtRule(prelude))).toEqual([
      breakpointMediaName(1, name),
    ]);
  });

  it('keeps every other component rule', async () => {
    expect(
      await lint(
        filePath,
        '@container (min-width: 48rem) {',
        '  .example {',
        '    padding: 8px;',
        '    color: #333;',
        '  }',
        '}',
        '@media (--narrow) {',
        '  .other {',
        '    background: rgb(0 0 0);',
        '  }',
        '}',
      ),
    ).toEqual([
      componentAtRule(1, '@container'),
      componentValue(3, 'padding', '8px'),
      hexColor(4, '#333'),
      componentValue(4, 'color', '#333'),
      componentCustomMedia(7, '--narrow'),
      componentValue(9, 'background', 'rgb(0 0 0)'),
      colourFunction(9, 'rgb'),
    ]);
  });

  it.each([
    '@media (max-width: 48rem)',
    '@media (width < 48rem)',
    '@media (48rem <= width)',
    '@media (min-height: 48rem)',
    '@media (prefers-reduced-motion: reduce)',
    '@media (MAX-WIDTH: 48rem)',
    '@media (max-width: 48REM)',
  ])('allows %s', async (prelude) => {
    expect(await lint(filePath, ...blockAtRule(prelude))).toEqual([]);
  });
});

describe('tokens.css (AAP 0.4.3)', () => {
  it('allows literal values and the reduced-motion !important override', async () => {
    expect(
      await lint(
        TOKENS_CSS,
        ':root {',
        '  --color-text: #1a1d23;',
        '}',
        '@media (prefers-reduced-motion: reduce) {',
        '  * {',
        '    animation-duration: 0.01ms !important;',
        '  }',
        '}',
      ),
    ).toEqual([]);
  });
});

describe('committed design-system stylesheets', () => {
  it.each(COMMITTED_STYLESHEETS)('%s lints clean', async (filePath) => {
    const source = readFileSync(new URL(filePath, WEB_URL), 'utf8');
    expect(await lint(filePath, source)).toEqual([]);
  });
});
