// @vitest-environment node
/**
 * Structural tests for tokens.css, the stylesheet that holds every placeholder token value, and
 * for its typed mirror, tokens.ts.
 *
 * The file is read as text and split into blocks and declarations by a small scanner that
 * understands comments, strings and nesting, which is all this stylesheet uses. The assertions
 * hold it to the semantic token contract (AAP 0.4.3) and to the promises its header makes.
 * The contract list below is kept independently of tokens.ts on purpose: tokens.ts is checked
 * against it too, so renaming a token in both files at once still fails here.
 */
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { TOKEN_NAMES as MIRRORED_TOKEN_NAMES, tokens } from './tokens';

const SOURCE = readFileSync(new URL('./tokens.css', import.meta.url), 'utf8');

/** The comment every token value carries until Blitzy OS values replace it (AAP 0.4.3). */
const PLACEHOLDER_MARKER = '/* GAP: placeholder, not Blitzy OS */';

/** The semantic token names, in contract order (AAP 0.4.3). */
const TOKEN_NAMES = [
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

const REDUCED_MOTION_PRELUDE = /^@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)$/;

/** One `property: value` declaration. */
interface Declaration {
  readonly property: string;
  /** The value without its `!important` flag, trimmed. */
  readonly value: string;
  readonly important: boolean;
  /** 1-based line of the `;` or `}` that ends the declaration. */
  readonly endLine: number;
}

/** One `{ … }` block: a style rule or an at-rule. */
interface Block {
  /** The selector or at-rule prelude, whitespace collapsed. */
  readonly prelude: string;
  /** Preludes of the enclosing blocks, outermost first. */
  readonly ancestors: readonly string[];
  readonly declarations: Declaration[];
}

/** Returns the offset just past the string literal that opens at `start`. */
function stringEnd(css: string, start: number): number {
  const quote = css[start];
  let i = start + 1;
  while (i < css.length) {
    const ch = css[i];
    if (ch === '\\') {
      i += 2;
    } else if (ch === quote) {
      return i + 1;
    } else if (ch === '\n') {
      break;
    } else {
      i += 1;
    }
  }
  throw new Error(`Unterminated string at offset ${String(start)}`);
}

/** Replaces every comment with spaces, keeping newlines, so offsets and lines are unchanged. */
function blankComments(css: string): string {
  let out = '';
  let i = 0;
  while (i < css.length) {
    const ch = css[i];
    if (ch === '"' || ch === "'") {
      const end = stringEnd(css, i);
      out += css.slice(i, end);
      i = end;
    } else if (css.startsWith('/*', i)) {
      const close = css.indexOf('*/', i + 2);
      if (close === -1) {
        throw new Error(`Unterminated comment at offset ${String(i)}`);
      }
      out += css.slice(i, close + 2).replace(/[^\n]/g, ' ');
      i = close + 2;
    } else {
      out += ch;
      i += 1;
    }
  }
  return out;
}

/** 1-based line number of `offset` in `css`. */
function lineAt(css: string, offset: number): number {
  return css.slice(0, offset).split('\n').length;
}

/** Parses the declaration text that ends at `endOffset`. */
function parseDeclaration(css: string, text: string, endOffset: number): Declaration {
  const colon = text.indexOf(':');
  if (colon === -1) {
    const line = String(lineAt(css, endOffset));
    throw new Error(`Malformed declaration "${text.trim()}" on line ${line}`);
  }
  const rawValue = text.slice(colon + 1).trim();
  const important = /!\s*important$/i.test(rawValue);
  return {
    property: text.slice(0, colon).trim(),
    value: rawValue.replace(/!\s*important$/i, '').trim(),
    important,
    endLine: lineAt(css, endOffset),
  };
}

/** Splits comment-free CSS into blocks, in source order. */
function parseBlocks(css: string): Block[] {
  const blocks: Block[] = [];
  const open: Block[] = [];
  let segmentStart = 0;
  let i = 0;
  while (i < css.length) {
    const ch = css[i];
    if (ch === '"' || ch === "'") {
      i = stringEnd(css, i);
      continue;
    }
    if (ch === '{' || ch === ';' || ch === '}') {
      const segment = css.slice(segmentStart, i);
      const current = open.at(-1);
      if (ch === '{') {
        const block: Block = {
          prelude: segment.trim().replace(/\s+/g, ' '),
          ancestors: open.map((parent) => parent.prelude),
          declarations: [],
        };
        blocks.push(block);
        open.push(block);
      } else if (segment.trim() !== '') {
        if (current === undefined) {
          throw new Error(`Statement outside any block on line ${String(lineAt(css, i))}`);
        }
        current.declarations.push(parseDeclaration(css, segment, i));
      }
      if (ch === '}') {
        if (open.pop() === undefined) {
          throw new Error(`Unbalanced "}" on line ${String(lineAt(css, i))}`);
        }
      }
      segmentStart = i + 1;
    }
    i += 1;
  }
  if (open.length > 0 || css.slice(segmentStart).trim() !== '') {
    throw new Error('tokens.css ends inside a block or with an unterminated statement');
  }
  return blocks;
}

const STRIPPED = blankComments(SOURCE);
const BLOCKS = parseBlocks(STRIPPED);
const SOURCE_LINES = SOURCE.split('\n');

const isCustomProperty = (declaration: Declaration): boolean =>
  declaration.property.startsWith('--');

const isReducedMotion = (block: Block): boolean =>
  [...block.ancestors, block.prelude].some((prelude) => REDUCED_MOTION_PRELUDE.test(prelude));

/** Custom properties declared in the top-level `:root` block, in source order. */
const ROOT_TOKENS = BLOCKS.filter(
  (block) => block.prelude === ':root' && block.ancestors.length === 0,
)
  .flatMap((block) => block.declarations)
  .filter(isCustomProperty);

describe('tokens.css', () => {
  it('opens with a header comment that states every value is a placeholder', () => {
    expect(SOURCE.startsWith('/*')).toBe(true);
    const header = SOURCE.slice(0, SOURCE.indexOf('*/') + 2)
      .replace(/^\s*\/?\*+\/?/gm, ' ')
      .replace(/\s+/g, ' ');
    expect(header).toContain('every value below is a neutral placeholder, not a Blitzy OS value');
    expect(header).toContain('replace the values in this file only');
  });

  it('declares exactly the semantic tokens, in contract order, on a top-level :root block', () => {
    expect(ROOT_TOKENS.map((declaration) => declaration.property)).toEqual([...TOKEN_NAMES]);
  });

  it('declares custom properties nowhere but :root', () => {
    const elsewhere = BLOCKS.filter((block) => block.prelude !== ':root').flatMap((block) =>
      block.declarations
        .filter(isCustomProperty)
        .map((declaration) => `${declaration.property} in "${block.prelude}"`),
    );
    expect(elsewhere).toEqual([]);
  });

  it('declares each custom property once per block', () => {
    const duplicates = BLOCKS.flatMap((block) => {
      const names = block.declarations.filter(isCustomProperty).map((d) => d.property);
      return names
        .filter((name, index) => names.indexOf(name) !== index)
        .map((name) => `${name} in "${block.prelude}"`);
    });
    expect(duplicates).toEqual([]);
  });

  it('gives every declaration a value', () => {
    const empty = BLOCKS.flatMap((block) =>
      block.declarations
        .filter((declaration) => declaration.value === '')
        .map((declaration) => `${declaration.property} on line ${String(declaration.endLine)}`),
    );
    expect(empty).toEqual([]);
  });

  it(`marks every token value with ${PLACEHOLDER_MARKER}`, () => {
    const unmarked = ROOT_TOKENS.filter((declaration) => {
      const line = SOURCE_LINES[declaration.endLine - 1] ?? '';
      return !line.trimEnd().endsWith(`; ${PLACEHOLDER_MARKER}`);
    }).map((declaration) => declaration.property);
    expect(ROOT_TOKENS).toHaveLength(TOKEN_NAMES.length);
    expect(unmarked).toEqual([]);
  });

  it('resolves every var() reference to a token declared in this file', () => {
    const declared = new Set(
      BLOCKS.flatMap((block) => block.declarations.filter(isCustomProperty).map((d) => d.property)),
    );
    const references = BLOCKS.flatMap((block) =>
      block.declarations.flatMap((declaration) =>
        [...declaration.value.matchAll(/var\(\s*(--[\w-]+)/g)].map((match) => match[1] ?? ''),
      ),
    );
    expect(references.length).toBeGreaterThan(0);
    expect(references.filter((name) => !declared.has(name))).toEqual([]);
  });

  it('uses !important only inside the prefers-reduced-motion override', () => {
    const important = BLOCKS.flatMap((block) =>
      block.declarations
        .filter((declaration) => declaration.important)
        .map((declaration) => ({ block, declaration })),
    );
    const outside = important
      .filter(({ block }) => !isReducedMotion(block))
      .map(({ block, declaration }) => `${declaration.property} in "${block.prelude}"`);
    expect(outside).toEqual([]);

    // The scanner saw every !important outside comments, so the check above covers them all.
    expect(STRIPPED.match(/!\s*important/gi) ?? []).toHaveLength(important.length);

    // Inside the override every declaration is !important, because it must win over component
    // animation declarations of any specificity.
    const override = BLOCKS.filter(isReducedMotion).flatMap((block) => block.declarations);
    expect(override.length).toBeGreaterThan(0);
    expect(override.filter((declaration) => !declaration.important)).toEqual([]);
  });
});

/** Every string leaf of a token tree, depth first, in property order. */
function tokenLeaves(tree: unknown): string[] {
  if (typeof tree === 'string') {
    return [tree];
  }
  if (typeof tree !== 'object' || tree === null) {
    throw new Error(`Unexpected token leaf of type ${typeof tree}`);
  }
  return Object.values(tree).flatMap(tokenLeaves);
}

describe('tokens.ts', () => {
  it('mirrors the contract names one-to-one, in tokens.css order', () => {
    expect([...MIRRORED_TOKEN_NAMES]).toEqual([...TOKEN_NAMES]);
  });

  it('references every token exactly once, in contract order, as a bare var()', () => {
    expect(tokenLeaves(tokens)).toEqual(TOKEN_NAMES.map((name) => `var(${name})`));
  });
});
