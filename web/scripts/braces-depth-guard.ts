/**
 * Runtime guard for GHSA-vfj7-8cjw-p6xm (CVE-2026-93687, CWE-674): uncontrolled recursion in
 * braces 3.0.3.
 *
 * braces parses the `{…}` and `(…)` groups of a glob pattern into a nested syntax tree, and its
 * compile, expand and stringify walkers recurse once per nesting level with no depth limit. Its
 * only bound is a 10,000-character input limit, so a pattern under that limit can nest deep
 * enough to exhaust the call stack: 4,999 `{` around `a` and 4,999 `}`, 9,999 characters in all,
 * ends in `RangeError: Maximum call stack size exceeded`. web/package-lock.json locks braces
 * 3.0.3 as a development dependency, reached only through stylelint (braces ← micromatch ←
 * fast-glob, globby, stylelint), so every glob stylelint receives, `npm run lint -- <glob>`
 * included, reaches it.
 *
 * Why a guard and not an upgrade: no fixed release exists. braces 3.0.3 is the latest release,
 * the latest micromatch (4.0.8) still depends on `braces ^3.0.3`, the advisory's
 * `first_patched_version` is null, and the upstream issue (micromatch/braces#70) is open with
 * only unmerged pull requests, so neither an upgrade nor an npm `overrides` entry has a version
 * to point to. `npm audit fix` offers only stylelint 7.7.0, a major downgrade that would break
 * the `stylelint ~17.16.0` pin. The lockfile therefore keeps braces 3.0.3, and this module
 * applies the fix the advisory recommends, a nesting-depth limit in `parse()`, to the installed
 * braces at load time.
 *
 * How it reaches every pattern: braces(), `braces.compile`, `braces.expand`,
 * `braces.stringify` and `braces.create` all reach the parser only through the exported
 * function's `parse` property, read at call time, so replacing that one property guards them
 * all. braces is resolved from micromatch's own location, exactly as micromatch requires it,
 * and the lockfile holds a single braces install, so the patched module is the one stylelint
 * uses. The depth is measured on the syntax tree braces itself builds, never by scanning
 * characters, because braces reads `[…]` and quoted text literally and flattens unclosed
 * groups; only its own tree says how deep the walkers will recurse. braces' parser is iterative,
 * so measuring the tree is safe at any depth below its character limit.
 *
 * Importing this module installs the guard: `npm run lint` preloads it into the stylelint
 * process with `node --import ./scripts/braces-depth-guard.ts`. A pattern over a bound is
 * refused with a `SyntaxError`, the error class braces raises for its own length limit, that
 * names the bound, the pattern and the advisory. The module is plain erasable TypeScript, which
 * Node runs through its built-in type stripping.
 *
 * Delete this module, and the `--import` in web/package.json, once braces publishes a release
 * that fixes the advisory and the lockfile locks it.
 */
import { createRequire } from 'node:module';

/** The advisory this guard mitigates, named in every refusal. */
const ADVISORY = 'GHSA-vfj7-8cjw-p6xm';

/**
 * The deepest nesting of `{…}` and `(…)` groups a pattern may have. The committed patterns nest
 * at most one level (as in `*.{css,scss}`), while braces exhausts Node's default stack only at a
 * few thousand levels, so 10 admits any real pattern and stays two orders of magnitude below the
 * crash.
 */
export const MAX_NESTING_DEPTH = 10;

/**
 * The longest pattern, in characters: a tenth of braces' own 10,000-character input limit. A
 * nested group costs at least two characters, so this bound alone also caps any pattern at 500
 * levels, and it leaves ample room for the absolute directory stylelint prefixes to
 * configuration patterns, while keeping them far below 10,000.
 */
export const MAX_PATTERN_LENGTH = 1_000;

/**
 * The longest pattern excerpt a refusal quotes, so the message stays readable for a pattern
 * thousands of characters long.
 */
const PREVIEW_LENGTH = 60;

/**
 * The property under which a guarded `parse` keeps the parser it wraps. A registered symbol, so
 * every instance of this module, in any module graph of one process, recognises an installed
 * guard and never wraps it a second time.
 */
const ORIGINAL_PARSE = Symbol.for('ads-engine-web.braces-depth-guard.original-parse');

/** braces' `parse(input, options)`: the syntax tree of a pattern. */
type BracesParse = (input: unknown, options?: unknown) => unknown;

/** The part of the braces export this guard relies on: its `parse` property. */
interface BracesModule {
  parse: BracesParse;
}

/**
 * Whether `value` has a callable `parse` property, as the braces export does.
 *
 * @param value - The loaded module, or anything else.
 * @returns True for a function or object whose `parse` is a function.
 */
function isBracesModule(value: unknown): value is BracesModule {
  return (
    (typeof value === 'function' || (typeof value === 'object' && value !== null)) &&
    'parse' in value &&
    typeof value.parse === 'function'
  );
}

/**
 * The parser a guarded `parse` wraps.
 *
 * @param parse - The current `parse` property of braces.
 * @returns The wrapped parser when `parse` is this guard; undefined for any other function.
 */
function wrappedParser(parse: BracesParse): BracesParse | undefined {
  const original: unknown = Reflect.get(parse, ORIGINAL_PARSE);
  return typeof original === 'function' ? (original as BracesParse) : undefined;
}

/**
 * The child nodes of a braces syntax-tree node.
 *
 * @param node - A node, or any other value.
 * @returns Its `nodes` array; undefined when it has none, as text, comma and similar leaf nodes.
 */
function childNodes(node: unknown): readonly unknown[] | undefined {
  if (typeof node !== 'object' || node === null || !('nodes' in node)) {
    return undefined;
  }
  return Array.isArray(node.nodes) ? (node.nodes as unknown[]) : undefined;
}

/**
 * How deep braces' recursive walkers descend into a syntax tree: the root is at depth 0, and each
 * node with a `nodes` array, a `{…}` or `(…)` group, sits one level deeper than the node that
 * contains it. Only `nodes` is followed, never the `parent` and `prev` back-references braces
 * also sets, and the walk uses an explicit stack, so a tree thousands of levels deep is measured
 * without deep recursion.
 *
 * @param ast - The tree `braces.parse` returned.
 * @returns The depth of the deepest group; 0 for a pattern without groups.
 */
function treeDepth(ast: unknown): number {
  if (childNodes(ast) === undefined) {
    return 0;
  }
  let deepest = 0;
  const visited = new Set<unknown>();
  const pending: { node: unknown; depth: number }[] = [{ node: ast, depth: 0 }];
  for (let entry = pending.pop(); entry !== undefined; entry = pending.pop()) {
    if (visited.has(entry.node)) {
      continue;
    }
    visited.add(entry.node);
    deepest = Math.max(deepest, entry.depth);
    for (const child of childNodes(entry.node) ?? []) {
      if (childNodes(child) !== undefined) {
        pending.push({ node: child, depth: entry.depth + 1 });
      }
    }
  }
  return deepest;
}

/**
 * The violation a pattern of `length` characters commits, if any.
 *
 * @param length - The pattern's length.
 * @returns The violation phrase; undefined within {@link MAX_PATTERN_LENGTH}.
 */
function lengthViolation(length: number): string | undefined {
  return length > MAX_PATTERN_LENGTH
    ? `is ${String(length)} characters long, more than ${String(MAX_PATTERN_LENGTH)}`
    : undefined;
}

/**
 * The violation a syntax tree `depth` levels deep commits, if any.
 *
 * @param depth - The tree's depth.
 * @returns The violation phrase; undefined within {@link MAX_NESTING_DEPTH}.
 */
function depthViolation(depth: number): string | undefined {
  return depth > MAX_NESTING_DEPTH
    ? `nests ${String(depth)} groups deep, more than ${String(MAX_NESTING_DEPTH)}`
    : undefined;
}

/**
 * The error the guard throws for a refused pattern.
 *
 * @param pattern - The refused pattern.
 * @param violation - The bound it breaks, as {@link patternViolations} words it.
 * @returns A SyntaxError naming the bound, the pattern's first {@link PREVIEW_LENGTH}
 *   characters and the advisory.
 */
function refusal(pattern: string, violation: string): SyntaxError {
  const preview =
    pattern.length > PREVIEW_LENGTH ? `${pattern.slice(0, PREVIEW_LENGTH)}…` : pattern;
  return new SyntaxError(
    `Glob pattern ${JSON.stringify(preview)} ${violation}: refused by the braces depth guard, because braces 3.0.3 walks nested groups recursively and an unbounded pattern can exhaust the call stack (${ADVISORY})`,
  );
}

/**
 * Wraps braces' parser with the two bounds. A string longer than {@link MAX_PATTERN_LENGTH} is
 * refused before it is parsed. Anything else is parsed by `original`, which keeps braces' own
 * checks (a non-string input still raises its TypeError), and the tree is refused when it nests
 * deeper than {@link MAX_NESTING_DEPTH}; otherwise it is returned unchanged.
 *
 * @param original - braces' unguarded parser.
 * @returns The guarded parser, which records `original` under {@link ORIGINAL_PARSE}.
 */
function guardParse(original: BracesParse): BracesParse {
  const guarded: BracesParse = (input, options) => {
    if (typeof input === 'string') {
      const overLength = lengthViolation(input.length);
      if (overLength !== undefined) {
        throw refusal(input, overLength);
      }
    }
    const ast = original(input, options);
    const tooDeep = depthViolation(treeDepth(ast));
    if (tooDeep !== undefined) {
      throw refusal(String(input), tooDeep);
    }
    return ast;
  };
  Object.defineProperty(guarded, ORIGINAL_PARSE, { value: original });
  return guarded;
}

/**
 * Installs the guard on a braces module by replacing its `parse` property. Installing is
 * idempotent: a module whose `parse` is already guarded, by this or another instance of this
 * module, is left as it is.
 *
 * @param braces - The braces export, as `require('braces')` returns it.
 * @returns braces' unguarded parser, which the depth helpers measure patterns with.
 * @throws Error when `braces` has no callable `parse`, or the guarded parser cannot be set, so a
 *   missing guard never goes unnoticed.
 */
export function installBracesDepthGuard(braces: unknown): BracesParse {
  if (!isBracesModule(braces)) {
    throw new Error(
      `braces depth guard (${ADVISORY}): the loaded braces module has no parse() function to guard; review this guard against the installed braces release`,
    );
  }
  const current = braces.parse;
  const original = wrappedParser(current);
  if (original !== undefined) {
    return original;
  }
  const guarded = guardParse(current);
  braces.parse = guarded;
  if (braces.parse !== guarded) {
    throw new Error(
      `braces depth guard (${ADVISORY}): braces.parse could not be replaced, so the guard is not installed`,
    );
  }
  return current;
}

/**
 * Loads braces the way micromatch does: resolved from micromatch's own location, so this is the
 * module instance micromatch, and through it fast-glob, globby and stylelint, use.
 *
 * @returns The braces export.
 * @throws Error when micromatch or braces cannot be resolved from this package.
 */
function loadLockedBraces(): unknown {
  const requireFromHere = createRequire(import.meta.url);
  try {
    return createRequire(requireFromHere.resolve('micromatch'))('braces') as unknown;
  } catch (cause) {
    throw new Error(
      `braces depth guard (${ADVISORY}): cannot load braces through micromatch from ${import.meta.url}; run npm ci in web/`,
      { cause },
    );
  }
}

/** braces' unguarded parser, kept for measuring patterns without the guard's refusals. */
const unguardedParse = installBracesDepthGuard(loadLockedBraces());

/**
 * How deep braces nests a pattern's groups, measured on the syntax tree its unguarded parser
 * builds: what the walkers would recurse over, with `[…]` and quoted text read literally and
 * unclosed groups flattened, exactly as braces reads them.
 *
 * @param pattern - A glob pattern as braces receives it.
 * @returns The depth of the deepest `{…}` or `(…)` group; 0 for a pattern without groups.
 * @throws SyntaxError from braces itself for a pattern over its 10,000-character limit.
 */
export function bracesAstDepth(pattern: string): number {
  return treeDepth(unguardedParse(pattern));
}

/**
 * How the guard judges a pattern, in the order it applies the bounds: the length first, and the
 * depth only for a pattern within the length bound, since the guard refuses a longer one before
 * parsing it.
 *
 * @param pattern - A glob pattern as braces receives it.
 * @returns The bound the pattern breaks, as one phrase such as
 *   `nests 11 groups deep, more than 10`; empty when the guard accepts the pattern.
 */
export function patternViolations(pattern: string): string[] {
  const violation =
    lengthViolation(pattern.length) ?? depthViolation(bracesAstDepth(pattern));
  return violation === undefined ? [] : [violation];
}
