// @vitest-environment node
/**
 * Tests for the braces depth guard, web/scripts/braces-depth-guard.ts, which mitigates
 * GHSA-vfj7-8cjw-p6xm (CVE-2026-93687, CWE-674): braces 3.0.3, which web/package-lock.json locks
 * through micromatch 4.0.8 and which has no fixed release, walks nested `{…}` and `(…)` groups
 * recursively, so a deeply nested glob pattern under its 10,000-character limit can exhaust the
 * call stack. The guard module records why a guard stands in for an upgrade.
 *
 * These tests establish that:
 * - the hazard is real in the locked braces, and the preloaded guard turns it into a refusal;
 * - the guard measures depth on braces' own syntax tree, so a closer braces reads as literal
 *   text, inside `[…]` or quotes, cannot hide nesting;
 * - the guard is installed, once, on the braces instance micromatch loads, and every braces
 *   entry point goes through it;
 * - every stylelint command in web/package.json `scripts` preloads the guard, and the committed
 *   lint command refuses a deep pattern appended to it, as `npm run lint -- <glob>` appends one,
 *   while as committed it lints every stylesheet and passes;
 * - web/.stylelintrc.json keeps its patterns within the bounds, for the stylelint runs inside the
 *   test process (src/stylelint-config.test.ts), where no preload is active;
 * - the lockfile installs braces once, the instance the guard patches, and only stylelint
 *   reaches it, so the stylelint command is the one that needs the guard.
 *
 * Delete this file with the guard once braces publishes a release that fixes the advisory.
 */
import { spawnSync } from 'node:child_process';
import { readdirSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join, posix, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  bracesAstDepth,
  installBracesDepthGuard,
  MAX_NESTING_DEPTH,
  MAX_PATTERN_LENGTH,
  patternViolations,
} from '../scripts/braces-depth-guard';

/** The web/ package root, which holds package.json, package-lock.json and .stylelintrc.json. */
const WEB_DIR = fileURLToPath(new URL('..', import.meta.url));

/** The guard module as web/package.json scripts preload it, relative to web/. */
const GUARD_SPECIFIER = './scripts/braces-depth-guard.ts';

/** The guard module's absolute path. */
const GUARD_PATH = resolve(WEB_DIR, GUARD_SPECIFIER);

/** The stylelint configuration `npm run lint` reads. */
const STYLELINT_CONFIG_PATH = join(WEB_DIR, '.stylelintrc.json');

/** The advisory every refusal names. */
const ADVISORY = 'GHSA-vfj7-8cjw-p6xm';

/** What a stack exhaustion reports; it must never surface while the guard is loaded. */
const STACK_OVERFLOW = 'Maximum call stack size exceeded';

/**
 * A pattern of `depth` nested groups around `a`.
 *
 * @param depth - How many groups to nest.
 * @param open - The opening character, `{` or `(`.
 * @param close - The matching closer.
 * @returns The pattern.
 */
function nested(depth: number, open = '{', close = '}'): string {
  return `${open.repeat(depth)}a${close.repeat(depth)}`;
}

/** The advisory's reproduction: 4,999 nested groups in 9,999 characters, under braces' limit. */
const DEEP_PATTERN = nested(4_999);

/** One group past the depth bound, and well within the length bound. */
const ONE_TOO_DEEP = nested(MAX_NESTING_DEPTH + 1);

/**
 * `count` groups, each opened by `{` and followed by a `}` inside `[…]`, which braces reads as
 * literal text, then `a` and `count` real closers.
 *
 * @param count - How many groups to open.
 * @returns The pattern, which braces nests `count` deep.
 */
function bracketedClosers(count: number): string {
  return `${'{[}]'.repeat(count)}a${'}'.repeat(count)}`;
}

/**
 * 501 characters that braces nests 100 groups deep, while a character scan that closes a group
 * at every `}` sees one level: the pattern an earlier, character-counting guard let through.
 */
const BRACKETED_CLOSERS = bracketedClosers(100);

/**
 * How long one stylelint run may take, in milliseconds: it loads every rule before it lints, which
 * takes a few seconds on a loaded host.
 */
const STYLELINT_RUN_TIMEOUT_MS = 90_000;

/** The budget of a test that runs stylelint, above {@link STYLELINT_RUN_TIMEOUT_MS}. */
const STYLELINT_TEST_TIMEOUT_MS = 120_000;

/** The characters a backslash escapes inside double quotes in sh. */
const DOUBLE_QUOTE_ESCAPES: ReadonlySet<string> = new Set(['"', '\\', '$', '`', '\n']);

/** The lockfile fields that list packages a lockfile entry loads. */
const DEPENDENCY_FIELDS = [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
] as const;

/**
 * V8's stack size, in KiB, for the child process that demonstrates the hazard: a quarter of the
 * 984 KiB default. Whether the default stack overflows on the reproduction depends on the Node
 * release and on how far the JIT has optimised braces (Node 22.23 always overflows, Node 24.21
 * only sometimes), so a fixed, smaller stack makes the demonstration deterministic. The controls
 * beside it, thousands of groups side by side in a pattern of the same length and a pattern at
 * the depth bound, expand on that same stack, so it is the nesting that exhausts it.
 */
const PROBE_STACK_SIZE_KIB = 250;

/**
 * The script the child process runs: it loads braces from the path in its first argument,
 * expands each pattern it reads as JSON from stdin with `{ expand: true }`, as fast-glob does,
 * and prints one outcome per pattern as JSON: `ok`, or the error's name and message.
 */
const BRACES_PROBE = [
  'const braces = require(process.argv[1]);',
  "const patterns = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));",
  'const outcomes = patterns.map((pattern) => {',
  '  try {',
  '    braces(pattern, { expand: true });',
  "    return 'ok';",
  '  } catch (error) {',
  "    return error instanceof Error ? `${error.name}: ${error.message}` : 'non-Error thrown';",
  '  }',
  '});',
  'process.stdout.write(JSON.stringify(outcomes));',
].join('\n');

/** A pattern from the stylelint configuration, with where it comes from for messages. */
interface SourcedPattern {
  readonly source: string;
  readonly pattern: string;
}

/** One shell command of a package.json script that runs the stylelint CLI. */
interface StylelintCommand {
  /** The name of the script it belongs to. */
  readonly script: string;
  /** The command's words, unquoted. */
  readonly words: readonly string[];
}

/** The braces installs in a lockfile and the packages that load them. */
interface BracesConsumers {
  /** The install path of every braces entry, sorted. */
  readonly installPaths: readonly string[];
  /** The name of every package that depends on braces, directly or transitively, sorted. */
  readonly dependents: readonly string[];
  /** The root's own dependencies through which it reaches braces, sorted. */
  readonly rootDependencies: readonly string[];
}

/** The braces API the in-process tests call, as `require('braces')` returns it. */
interface BracesApi {
  (pattern: string, options?: Readonly<Record<string, unknown>>): string[];
  parse: (pattern: unknown) => unknown;
  compile: (pattern: string) => unknown;
  expand: (pattern: string) => string[];
  stringify: (pattern: string) => string;
  create: (pattern: string, options?: Readonly<Record<string, unknown>>) => unknown;
}

/** The part of the micromatch API that hands patterns to braces. */
interface MicromatchApi {
  braces: (pattern: string, options?: Readonly<Record<string, unknown>>) => string[];
}

/**
 * Whether `value` is a plain object, as JSON objects parse.
 *
 * @param value - Any parsed JSON value.
 * @returns True for an object that is neither null nor an array.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Parses a JSON file.
 *
 * @param path - Absolute path of the file.
 * @returns The parsed value, to be narrowed by the caller.
 */
function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, 'utf8')) as unknown;
}

/**
 * Every bound broken by `patterns`, one line per violation, naming the pattern's source and its
 * first 60 characters. Each pattern is judged as written and as stylelint hands configuration
 * patterns on: stylelint runs them through normalize-path, which turns every backslash into `/`,
 * so an escape written in the configuration no longer escapes by the time braces parses it.
 *
 * @param patterns - Patterns collected from the stylelint configuration.
 * @returns The violation lines; empty when every pattern is within the bounds.
 */
function describeViolations(patterns: readonly SourcedPattern[]): string[] {
  return patterns.flatMap(({ source, pattern }) => {
    const preview = pattern.length > 60 ? `${pattern.slice(0, 60)}…` : pattern;
    const violations = new Set([
      ...patternViolations(pattern),
      ...patternViolations(pattern.replaceAll('\\', '/')),
    ]);
    return [...violations].map(
      (violation) => `${source}: ${JSON.stringify(preview)} ${violation}`,
    );
  });
}

/**
 * Splits a package.json script into its shell commands, each a list of words, as sh reads them:
 * a command ends at `&&`, `||`, `|`, `&`, `;` or a newline, and a word at unquoted whitespace.
 * Single quotes keep everything literally, double quotes keep everything except a backslash
 * before `"`, `\`, `$`, a backtick or a newline, and an unquoted backslash makes the next
 * character literal.
 *
 * @param script - The script's command line.
 * @returns The commands in order, each with its words unquoted.
 * @throws Error on an unterminated quote or a trailing backslash, so that no stylelint command
 *   is lost to a parse that gave up.
 */
function shellCommands(script: string): string[][] {
  const commands: string[][] = [];
  let words: string[] = [];
  let word = '';
  let inWord = false;
  let quote = '';
  const endWord = (): void => {
    if (inWord) {
      words.push(word);
    }
    word = '';
    inWord = false;
  };
  const endCommand = (): void => {
    endWord();
    if (words.length > 0) {
      commands.push(words);
    }
    words = [];
  };
  for (let index = 0; index < script.length; index += 1) {
    const char = script.charAt(index);
    const next = script.charAt(index + 1);
    if (quote === "'") {
      if (char === "'") {
        quote = '';
      } else {
        word += char;
      }
    } else if (quote === '"') {
      if (char === '"') {
        quote = '';
      } else if (char === '\\' && DOUBLE_QUOTE_ESCAPES.has(next)) {
        word += next;
        index += 1;
      } else {
        word += char;
      }
    } else if (char === '\\') {
      if (index + 1 >= script.length) {
        throw new Error(`Trailing backslash in script: ${script}`);
      }
      word += next;
      inWord = true;
      index += 1;
    } else if (char === "'" || char === '"') {
      quote = char;
      inWord = true;
    } else if (char === '&' || char === '|' || char === ';' || char === '\n') {
      endCommand();
      if ((char === '&' || char === '|') && next === char) {
        index += 1;
      }
    } else if (/\s/.test(char)) {
      endWord();
    } else {
      word += char;
      inWord = true;
    }
  }
  if (quote !== '') {
    throw new Error(`Unterminated ${quote} quote in script: ${script}`);
  }
  endCommand();
  return commands;
}

/**
 * Whether a command word names the stylelint CLI program.
 *
 * @param word - One word of a shell command.
 * @returns True for `stylelint`, a path ending in it such as `node_modules/.bin/stylelint`, and
 *   a path to its script such as `node_modules/stylelint/bin/stylelint.mjs`.
 */
function isStylelintProgram(word: string): boolean {
  return /(^|\/)stylelint(\.[cm]?js)?$/.test(word);
}

/**
 * Every shell command in a package.json `scripts` map that runs the stylelint CLI.
 *
 * @param manifest - The parsed package.json.
 * @returns The commands, in script order.
 * @throws Error when a script mentions stylelint but no stylelint command can be read from it,
 *   so a stylelint run is never missed because it was written in an unexpected form.
 */
function stylelintCommands(manifest: unknown): StylelintCommand[] {
  if (!isRecord(manifest)) {
    throw new Error('web/package.json is not a JSON object');
  }
  const scripts = manifest.scripts ?? {};
  if (!isRecord(scripts)) {
    throw new Error('web/package.json "scripts" is not an object');
  }
  return Object.entries(scripts).flatMap(([script, line]) => {
    if (typeof line !== 'string') {
      throw new Error(`web/package.json scripts.${script} is not a string`);
    }
    if (!line.includes('stylelint')) {
      return [];
    }
    const commands = shellCommands(line).filter((words) => words.some(isStylelintProgram));
    if (commands.length === 0) {
      throw new Error(
        `web/package.json scripts.${script} mentions stylelint, but no stylelint command could be read from it; check that it runs under the braces depth guard and teach this test to read it. Script: ${line}`,
      );
    }
    return commands.map((words) => ({ script, words }));
  });
}

/**
 * The words every stylelint command must start with: Node, preloading the guard, running the
 * script installed stylelint declares as its `stylelint` bin.
 *
 * @returns `node --import ./scripts/braces-depth-guard.ts node_modules/stylelint/<bin>`.
 * @throws Error when the installed stylelint declares no `stylelint` bin.
 */
function guardedStylelintPrefix(): readonly string[] {
  const manifest = readJson(join(WEB_DIR, 'node_modules', 'stylelint', 'package.json'));
  const bin = isRecord(manifest) && isRecord(manifest.bin) ? manifest.bin.stylelint : undefined;
  if (typeof bin !== 'string') {
    throw new Error('The installed stylelint declares no "stylelint" bin in its package.json');
  }
  return ['node', '--import', GUARD_SPECIFIER, posix.join('node_modules', 'stylelint', bin)];
}

/**
 * The stylelint commands that run without the guard preloaded.
 *
 * @param commands - The stylelint commands of a manifest.
 * @param prefix - The words a guarded command starts with.
 * @returns One message per unguarded command; empty when every command is guarded.
 */
function unguardedCommands(
  commands: readonly StylelintCommand[],
  prefix: readonly string[],
): string[] {
  return commands
    .filter(
      ({ words }) =>
        !prefix.every((word, index) => words[index] === word) ||
        words.findIndex(isStylelintProgram) !== prefix.length - 1,
    )
    .map(
      ({ script, words }) =>
        `web/package.json scripts.${script} runs ${JSON.stringify(words.join(' '))}, which bypasses the braces depth guard (${ADVISORY}); start it with "${prefix.join(' ')}"`,
    );
}

/**
 * The committed stylelint command of `npm run lint`.
 *
 * @returns Its words, unquoted.
 * @throws Error unless web/package.json `scripts.lint` holds exactly one stylelint command.
 */
function committedLintCommand(): readonly string[] {
  const commands = stylelintCommands(readJson(join(WEB_DIR, 'package.json'))).filter(
    ({ script }) => script === 'lint',
  );
  const [command] = commands;
  if (command === undefined || commands.length !== 1) {
    throw new Error(
      `web/package.json scripts.lint holds ${String(commands.length)} stylelint commands, not one`,
    );
  }
  return command.words;
}

/**
 * The patterns of a stylelint property that takes a string or an array of strings.
 *
 * @param value - The property's value.
 * @param source - Where the property sits, for labels and messages.
 * @returns One labelled pattern per string.
 * @throws Error when the value is neither a string nor an array of strings.
 */
function stringPatterns(value: unknown, source: string): SourcedPattern[] {
  if (typeof value === 'string') {
    return [{ source, pattern: value }];
  }
  if (Array.isArray(value)) {
    return value.map((item: unknown, index) => {
      if (typeof item !== 'string') {
        throw new Error(`${source}[${String(index)}] is not a string`);
      }
      return { source: `${source}[${String(index)}]`, pattern: item };
    });
  }
  throw new Error(`${source} is neither a string nor an array of strings`);
}

/**
 * The patterns a stylelint configuration passes on: `ignoreFiles`, `referenceFiles` (strings or
 * `{ files }` objects) and every `overrides[].files`, including the `ignoreFiles` and
 * `referenceFiles` of each override.
 *
 * @param config - The parsed configuration, or one of its overrides.
 * @param source - Where it sits, for labels and messages.
 * @returns The labelled patterns.
 * @throws Error on a property whose shape stylelint would not accept, so a pattern is never
 *   skipped because it was written in an unexpected form.
 */
function configPatterns(config: unknown, source: string): SourcedPattern[] {
  if (!isRecord(config)) {
    throw new Error(`${source} is not an object`);
  }
  const patterns: SourcedPattern[] = [];
  if (config.ignoreFiles !== undefined) {
    patterns.push(...stringPatterns(config.ignoreFiles, `${source} ignoreFiles`));
  }
  if (config.referenceFiles !== undefined) {
    const entries: unknown[] = Array.isArray(config.referenceFiles)
      ? config.referenceFiles
      : [config.referenceFiles];
    entries.forEach((entry, index) => {
      const at = `${source} referenceFiles[${String(index)}]`;
      patterns.push(
        ...(isRecord(entry)
          ? stringPatterns(entry.files, `${at}.files`)
          : stringPatterns(entry, at)),
      );
    });
  }
  if (config.overrides !== undefined) {
    if (!Array.isArray(config.overrides)) {
      throw new Error(`${source} overrides is not an array`);
    }
    config.overrides.forEach((override: unknown, index) => {
      const at = `${source} overrides[${String(index)}]`;
      if (!isRecord(override)) {
        throw new Error(`${at} is not an object`);
      }
      patterns.push(
        ...stringPatterns(override.files, `${at}.files`),
        ...configPatterns(override, at),
      );
    });
  }
  return patterns;
}

/**
 * The install path Node resolves `dependency` to from the package installed at `fromPath`: the
 * nearest `node_modules/<dependency>` in `fromPath` or one of its ancestors, then the top level,
 * following a workspace link to its target.
 *
 * @param packages - The lockfile's `packages` map.
 * @param fromPath - Install path of the depending package; `''` for the root.
 * @param dependency - The dependency's package name.
 * @returns The resolved install path, or undefined when the dependency is not installed (an
 *   optional or peer dependency left out).
 */
function resolveInstallPath(
  packages: Record<string, unknown>,
  fromPath: string,
  dependency: string,
): string | undefined {
  let base = fromPath;
  for (;;) {
    const candidate = `${base === '' ? '' : `${base}/`}node_modules/${dependency}`;
    const entry = packages[candidate];
    if (entry !== undefined) {
      return isRecord(entry) && entry.link === true && typeof entry.resolved === 'string'
        ? entry.resolved
        : candidate;
    }
    if (base === '') {
      return undefined;
    }
    const parent = base.lastIndexOf('/node_modules/');
    base = parent < 0 ? '' : base.slice(0, parent);
  }
}

/**
 * The package name of the lockfile entry at `path`.
 *
 * @param path - The entry's install path.
 * @param entry - The entry itself.
 * @returns Its `name` field (set for aliases and links), else the path after the last
 *   `node_modules/`.
 */
function packageName(path: string, entry: unknown): string {
  if (isRecord(entry) && typeof entry.name === 'string') {
    return entry.name;
  }
  const at = path.lastIndexOf('node_modules/');
  return at < 0 ? path : path.slice(at + 'node_modules/'.length);
}

/**
 * The braces installs in a lockfile and the packages that load them, found by resolving every
 * declared dependency the way Node does and walking the resulting graph back from each
 * installed braces.
 *
 * @param lock - The parsed package-lock.json (lockfile version 2 or later).
 * @returns The braces install paths, their dependents and the root dependencies that lead to
 *   braces.
 * @throws Error when the lockfile has no `packages` map or an entry is malformed.
 */
function bracesConsumers(lock: unknown): BracesConsumers {
  if (!isRecord(lock) || !isRecord(lock.packages)) {
    throw new Error('package-lock.json has no "packages" map (lockfile version 2 or later)');
  }
  const packages = lock.packages;
  const dependentsOf = new Map<string, Set<string>>();
  const rootTargets = new Map<string, string>();
  for (const [path, entry] of Object.entries(packages)) {
    if (!isRecord(entry)) {
      throw new Error(`package-lock.json packages["${path}"] is not an object`);
    }
    for (const field of DEPENDENCY_FIELDS) {
      const declared = entry[field];
      if (declared === undefined) {
        continue;
      }
      if (!isRecord(declared)) {
        throw new Error(`package-lock.json packages["${path}"].${field} is not an object`);
      }
      for (const dependency of Object.keys(declared)) {
        const target = resolveInstallPath(packages, path, dependency);
        if (target === undefined) {
          continue;
        }
        const dependents = dependentsOf.get(target) ?? new Set<string>();
        dependents.add(path);
        dependentsOf.set(target, dependents);
        if (path === '') {
          rootTargets.set(target, dependency);
        }
      }
    }
  }
  const bracesPaths = Object.keys(packages).filter(
    (path) => path !== '' && packageName(path, packages[path]) === 'braces',
  );
  const reached = new Set<string>();
  const queue = [...bracesPaths];
  for (let target = queue.pop(); target !== undefined; target = queue.pop()) {
    for (const dependent of dependentsOf.get(target) ?? []) {
      if (!reached.has(dependent)) {
        reached.add(dependent);
        queue.push(dependent);
      }
    }
  }
  const dependents = [...reached]
    .filter((path) => path !== '')
    .map((path) => packageName(path, packages[path]));
  const rootDependencies = [...rootTargets]
    .filter(([target]) => reached.has(target) || bracesPaths.includes(target))
    .map(([, dependency]) => dependency);
  return {
    installPaths: [...bracesPaths].sort(),
    dependents: [...new Set(dependents)].sort(),
    rootDependencies: [...new Set(rootDependencies)].sort(),
  };
}

/**
 * The braces module micromatch loads, resolved from micromatch's own location as Node does, and
 * as the guard resolves it.
 *
 * @returns The absolute path of braces' entry file.
 */
function lockedBracesPath(): string {
  const requireFromWeb = createRequire(import.meta.url);
  return createRequire(requireFromWeb.resolve('micromatch')).resolve('braces');
}

/**
 * Expands each pattern with the locked braces in a child Node process whose stack is
 * {@link PROBE_STACK_SIZE_KIB} KiB, so a stack overflow is contained and deterministic.
 *
 * @param patterns - The patterns to expand.
 * @param guarded - Whether the child preloads the guard with `--import`, as `npm run lint` does.
 * @returns One outcome per pattern: `ok`, or the thrown error's name and message.
 * @throws Error when the child process fails or prints something other than the outcomes.
 */
function expandInChild(patterns: readonly string[], guarded: boolean): string[] {
  const preload = guarded ? ['--import', pathToFileURL(GUARD_PATH).href] : [];
  const child = spawnSync(
    process.execPath,
    [
      `--stack-size=${String(PROBE_STACK_SIZE_KIB)}`,
      ...preload,
      '--input-type=commonjs',
      '-e',
      BRACES_PROBE,
      lockedBracesPath(),
    ],
    { input: JSON.stringify(patterns), encoding: 'utf8', timeout: 30_000 },
  );
  if (child.error !== undefined || child.status !== 0) {
    const reason = child.error?.message ?? child.stderr;
    throw new Error(`The braces probe failed (status ${String(child.status)}): ${reason}`);
  }
  const outcomes = JSON.parse(child.stdout) as unknown;
  if (
    !Array.isArray(outcomes) ||
    !outcomes.every((outcome): outcome is string => typeof outcome === 'string')
  ) {
    throw new Error(`The braces probe printed ${child.stdout}`);
  }
  return outcomes;
}

/**
 * braces' own input limit, read from the locked release.
 *
 * @returns `MAX_LENGTH` from braces' constants module.
 * @throws Error when the locked braces no longer exposes it as a number.
 */
function lockedBracesMaxLength(): number {
  const constants = createRequire(lockedBracesPath())('./lib/constants.js') as unknown;
  if (!isRecord(constants) || typeof constants.MAX_LENGTH !== 'number') {
    throw new Error(
      'The locked braces no longer exports a numeric MAX_LENGTH from lib/constants.js',
    );
  }
  return constants.MAX_LENGTH;
}

/**
 * Runs a stylelint command from web/package.json in web/, as npm runs scripts, with `extra`
 * appended, as `npm run lint -- <args>` appends them. `node` is the running Node, the release
 * `npm run lint` finds first on PATH.
 *
 * @param words - The command's words.
 * @param extra - Arguments to append.
 * @returns The exit status (null when a signal ended the run) and everything written to stdout
 *   and stderr.
 * @throws Error when the command is empty or stylelint cannot be run or does not finish within
 *   {@link STYLELINT_RUN_TIMEOUT_MS}.
 */
function runStylelint(
  words: readonly string[],
  extra: readonly string[],
): { status: number | null; stdout: string; stderr: string } {
  const [program, ...args] = words;
  if (program === undefined) {
    throw new Error('The stylelint command is empty');
  }
  const run = spawnSync(program === 'node' ? process.execPath : program, [...args, ...extra], {
    cwd: WEB_DIR,
    encoding: 'utf8',
    timeout: STYLELINT_RUN_TIMEOUT_MS,
  });
  if (run.error !== undefined) {
    throw new Error(`stylelint did not run to completion: ${run.error.message}`);
  }
  return { status: run.status, stdout: run.stdout, stderr: run.stderr };
}

/**
 * The stylesheets under web/src, which the committed lint glob names.
 *
 * @returns Their paths relative to web/, with `/` separators, sorted.
 */
function committedStylesheets(): string[] {
  return readdirSync(join(WEB_DIR, 'src'), { recursive: true, encoding: 'utf8' })
    .filter((path) => path.endsWith('.css'))
    .map((path) => `src/${path.split(sep).join('/')}`)
    .sort();
}

/**
 * The files a stylelint JSON report covers, each with its number of warnings.
 *
 * @param text - What `--formatter json` printed.
 * @returns One entry per linted file, its path relative to web/ with `/` separators.
 * @throws Error when the text is not a stylelint JSON report.
 */
function reportedFiles(text: string): { file: string; warnings: number }[] {
  const report = JSON.parse(text) as unknown;
  if (!Array.isArray(report)) {
    throw new Error(`stylelint printed no JSON report: ${text}`);
  }
  return report.map((result: unknown) => {
    if (!isRecord(result) || typeof result.source !== 'string' || !Array.isArray(result.warnings)) {
      throw new Error(`stylelint reported an unexpected result: ${JSON.stringify(result)}`);
    }
    return {
      file: relative(WEB_DIR, result.source).split(sep).join('/'),
      warnings: result.warnings.length,
    };
  });
}

/**
 * What `call` throws, as `<name>: <message>`, for asserting a refusal's class and wording at once.
 *
 * @param call - The call expected to throw.
 * @returns The thrown error's name and message; `returned` when nothing was thrown.
 */
function thrownBy(call: () => unknown): string {
  try {
    call();
  } catch (error) {
    return error instanceof Error ? `${error.name}: ${error.message}` : 'non-Error thrown';
  }
  return 'returned';
}

describe('braces depth guard (GHSA-vfj7-8cjw-p6xm)', () => {
  describe('bracesAstDepth', () => {
    it('counts nested {…} and (…) groups as braces builds them', () => {
      expect(bracesAstDepth('')).toBe(0);
      expect(bracesAstDepth('src/**/*.css')).toBe(0);
      expect(bracesAstDepth('src/**/*.{css,scss}')).toBe(1);
      expect(bracesAstDepth('{a}{b}{c}')).toBe(1);
      expect(bracesAstDepth('{a,{b,{c,d}}}')).toBe(3);
      expect(bracesAstDepth('@(a|(b|c))')).toBe(2);
      expect(bracesAstDepth('(((a)))')).toBe(3);
      expect(bracesAstDepth('{a,(b|{c,d})}')).toBe(3);
      // braces parses iteratively, so even the reproduction is measured without recursion.
      expect(bracesAstDepth(DEEP_PATTERN)).toBe(4_999);
    });

    it('counts groups whose closers braces reads as literal text inside […] or quotes', () => {
      expect(bracesAstDepth(BRACKETED_CLOSERS)).toBe(100);
      expect(bracesAstDepth(`${'{[[}]]'.repeat(11)}a${'}'.repeat(11)}`)).toBe(11);
      expect(bracesAstDepth(`${'{[\\]}]'.repeat(11)}a${'}'.repeat(11)}`)).toBe(11);
      expect(bracesAstDepth(`${'{"}"'.repeat(11)}a${'}'.repeat(11)}`)).toBe(11);
      expect(bracesAstDepth(`${"{'}'".repeat(11)}a${'}'.repeat(11)}`)).toBe(11);
      expect(bracesAstDepth(`${'{`}`'.repeat(11)}a${'}'.repeat(11)}`)).toBe(11);
    });

    it('honours backslash escapes', () => {
      expect(bracesAstDepth('\\{\\{a\\}\\}')).toBe(0);
      expect(bracesAstDepth('\\(\\(a\\)\\)')).toBe(0);
      // An escaped backslash leaves the brace after it unescaped.
      expect(bracesAstDepth('\\\\{a}')).toBe(1);
      // Escaped closers close nothing, and braces flattens the groups left open.
      expect(bracesAstDepth('{\\}{\\}')).toBe(0);
    });

    it('closes a group only with its own closer and flattens unclosed groups', () => {
      expect(bracesAstDepth('{a)b}')).toBe(1);
      expect(bracesAstDepth('(a}b)')).toBe(1);
      expect(bracesAstDepth('}}{a}')).toBe(1);
      expect(bracesAstDepth('{{{')).toBe(0);
      expect(bracesAstDepth('{{a}')).toBe(1);
    });
  });

  describe('patternViolations', () => {
    it('accepts ordinary patterns and patterns at the bounds', () => {
      for (const pattern of [
        'src/**/*.css',
        'src/**/*.{css,scss}',
        'src/features/**/*.module.css',
        '!**/node_modules/**',
        nested(MAX_NESTING_DEPTH),
        nested(MAX_NESTING_DEPTH, '(', ')'),
        bracketedClosers(MAX_NESTING_DEPTH),
        'a'.repeat(MAX_PATTERN_LENGTH),
      ]) {
        expect(patternViolations(pattern), pattern.slice(0, 60)).toEqual([]);
      }
    });

    it('refuses the advisory reproduction on length, before parsing it', () => {
      expect(patternViolations(DEEP_PATTERN)).toEqual(['is 9999 characters long, more than 1000']);
    });

    it('refuses a pattern one group over the depth bound', () => {
      expect(patternViolations(ONE_TOO_DEEP)).toEqual(['nests 11 groups deep, more than 10']);
      expect(patternViolations(nested(MAX_NESTING_DEPTH + 1, '(', ')'))).toEqual([
        'nests 11 groups deep, more than 10',
      ]);
      expect(patternViolations(`${'{"}"'.repeat(11)}a${'}'.repeat(11)}`)).toEqual([
        'nests 11 groups deep, more than 10',
      ]);
    });

    it('refuses closers hidden in […] that a character count misses', () => {
      expect(BRACKETED_CLOSERS).toHaveLength(501);
      expect(patternViolations(BRACKETED_CLOSERS)).toEqual(['nests 100 groups deep, more than 10']);
    });

    it('refuses a pattern one character over the length bound', () => {
      expect(patternViolations('a'.repeat(MAX_PATTERN_LENGTH + 1))).toEqual([
        'is 1001 characters long, more than 1000',
      ]);
    });
  });

  describe('the hazard in the locked braces', () => {
    it('admits the reproduction under its only bound, ten times the length bound', () => {
      const maxLength = lockedBracesMaxLength();
      expect(DEEP_PATTERN.length).toBeLessThan(maxLength);
      expect(MAX_PATTERN_LENGTH * 10).toBeLessThanOrEqual(maxLength);
    });

    it('overflows the stack on deep nesting, while sibling and bounded groups expand', () => {
      // 3,333 groups side by side: as long as the reproduction, but only one level deep.
      const siblingsOfSameLength = '{a}'.repeat(3_333);
      expect(siblingsOfSameLength.length).toBe(DEEP_PATTERN.length);
      expect(
        expandInChild([DEEP_PATTERN, siblingsOfSameLength, nested(MAX_NESTING_DEPTH)], false),
      ).toEqual([`RangeError: ${STACK_OVERFLOW}`, 'ok', 'ok']);
    });

    it('is refused with a SyntaxError, never a RangeError, once the guard is preloaded', () => {
      const outcomes = expandInChild(
        [
          DEEP_PATTERN,
          ONE_TOO_DEEP,
          BRACKETED_CLOSERS,
          nested(MAX_NESTING_DEPTH),
          '{a}'.repeat(333),
        ],
        true,
      );
      expect(outcomes).toHaveLength(5);
      const [deep, oneTooDeep, bracketed, ...accepted] = outcomes;
      expect(deep).toMatch(/^SyntaxError: .* is 9999 characters long, more than 1000: /);
      expect(oneTooDeep).toMatch(/^SyntaxError: .* nests 11 groups deep, more than 10: /);
      expect(bracketed).toMatch(/^SyntaxError: .* nests 100 groups deep, more than 10: /);
      for (const refusal of [deep, oneTooDeep, bracketed]) {
        expect(refusal).toContain(ADVISORY);
      }
      expect(accepted).toEqual(['ok', 'ok']);
    });
  });

  describe('the guard in this process', () => {
    const requireFromWeb = createRequire(import.meta.url);
    const braces = createRequire(requireFromWeb.resolve('micromatch'))('braces') as BracesApi;
    const micromatch = requireFromWeb('micromatch') as MicromatchApi;

    it('is installed once on the braces instance micromatch loads', () => {
      const guarded = braces.parse;
      const unguarded = installBracesDepthGuard(braces);
      expect(unguarded).not.toBe(guarded);
      expect(braces.parse).toBe(guarded);
      expect(installBracesDepthGuard(braces)).toBe(unguarded);
      expect(braces.parse).toBe(guarded);
    });

    it('fails closed on a module it cannot guard', () => {
      expect(() => installBracesDepthGuard({})).toThrow('no parse() function to guard');
      expect(() => installBracesDepthGuard(null)).toThrow('no parse() function to guard');
    });

    it('refuses a deep pattern at every braces entry point', () => {
      const entryPoints: Record<string, (pattern: string) => unknown> = {
        braces: (pattern) => braces(pattern),
        'braces with expand': (pattern) => braces(pattern, { expand: true }),
        'braces.parse': (pattern) => braces.parse(pattern),
        'braces.compile': (pattern) => braces.compile(pattern),
        'braces.expand': (pattern) => braces.expand(pattern),
        'braces.stringify': (pattern) => braces.stringify(pattern),
        'braces.create': (pattern) => braces.create(pattern),
        'micromatch.braces': (pattern) => micromatch.braces(pattern, { expand: true }),
      };
      for (const [name, call] of Object.entries(entryPoints)) {
        expect(thrownBy(() => call(BRACKETED_CLOSERS)), name).toMatch(
          /^SyntaxError: .* nests 100 groups deep, more than 10: .*GHSA-vfj7-8cjw-p6xm/,
        );
        expect(thrownBy(() => call(DEEP_PATTERN)), name).toMatch(
          /^SyntaxError: .* is 9999 characters long, more than 1000: .*GHSA-vfj7-8cjw-p6xm/,
        );
      }
    });

    it('passes ordinary patterns, and braces’ own errors, through unchanged', () => {
      expect(braces('src/**/*.{css,scss}', { expand: true })).toEqual([
        'src/**/*.css',
        'src/**/*.scss',
      ]);
      expect(braces('src/**/*.{css,scss}')).toEqual(['src/**/*.(css|scss)']);
      expect(braces.expand('{a,{b,c}}')).toEqual(['a', 'b', 'c']);
      expect(thrownBy(() => braces.parse(42))).toBe('TypeError: Expected a string');
    });
  });

  describe('script parsing', () => {
    it('reads the lint script as sh does', () => {
      expect(
        shellCommands(
          'eslint . --max-warnings 0 && node --import ./scripts/braces-depth-guard.ts node_modules/stylelint/bin/stylelint.mjs "src/**/*.css" --allow-empty-input',
        ),
      ).toEqual([
        ['eslint', '.', '--max-warnings', '0'],
        [
          'node',
          '--import',
          './scripts/braces-depth-guard.ts',
          'node_modules/stylelint/bin/stylelint.mjs',
          'src/**/*.css',
          '--allow-empty-input',
        ],
      ]);
    });

    it('handles quotes, escapes and every command separator', () => {
      expect(
        shellCommands(
          'A=1 npx stylelint \'a b/*.css\' c\\ d.css "e\\"f" ""; ' +
            './node_modules/.bin/stylelint x || y | z & w\nv',
        ),
      ).toEqual([
        ['A=1', 'npx', 'stylelint', 'a b/*.css', 'c d.css', 'e"f', ''],
        ['./node_modules/.bin/stylelint', 'x'],
        ['y'],
        ['z'],
        ['w'],
        ['v'],
      ]);
    });

    it('rejects an unterminated quote and a trailing backslash', () => {
      expect(() => shellCommands('stylelint "src/*.css')).toThrow('Unterminated " quote');
      expect(() => shellCommands("stylelint 'src/*.css")).toThrow("Unterminated ' quote");
      expect(() => shellCommands('stylelint src/*.css \\')).toThrow('Trailing backslash');
    });
  });

  describe('stylelint commands', () => {
    const prefix = guardedStylelintPrefix();
    const guarded = `${prefix.join(' ')} "src/**/*.css"`;

    it('start with the guard preload and the bin stylelint declares', () => {
      expect(prefix).toEqual([
        'node',
        '--import',
        './scripts/braces-depth-guard.ts',
        'node_modules/stylelint/bin/stylelint.mjs',
      ]);
    });

    it('accept only a run that preloads the guard', () => {
      const commands = stylelintCommands({
        scripts: {
          build: 'vite build',
          lint: `eslint . && ${guarded}`,
          bare: 'stylelint "src/**/*.css"',
          npx: 'npx stylelint "src/**/*.css"',
          bin: './node_modules/.bin/stylelint "src/**/*.css"',
          direct: 'node node_modules/stylelint/bin/stylelint.mjs "src/**/*.css"',
          other: 'node --import ./other.ts node_modules/stylelint/bin/stylelint.mjs x.css',
          late: `${guarded} && stylelint again.css`,
        },
      });
      expect(commands.map(({ script }) => script)).toEqual([
        'lint',
        'bare',
        'npx',
        'bin',
        'direct',
        'other',
        'late',
        'late',
      ]);
      const unguarded = unguardedCommands(commands, prefix);
      expect(unguarded.map((message) => message.split(' runs ')[0])).toEqual([
        'web/package.json scripts.bare',
        'web/package.json scripts.npx',
        'web/package.json scripts.bin',
        'web/package.json scripts.direct',
        'web/package.json scripts.other',
        'web/package.json scripts.late',
      ]);
      for (const message of unguarded) {
        expect(message).toContain(`bypasses the braces depth guard (${ADVISORY})`);
      }
    });

    it('fail loudly wherever a stylelint run could be missed', () => {
      const read = (line: unknown) => () => stylelintCommands({ scripts: { css: line } });
      expect(read('echo "run stylelint later"')).toThrow('no stylelint command could be read');
      expect(read(1)).toThrow('web/package.json scripts.css is not a string');
      expect(() => stylelintCommands([])).toThrow('web/package.json is not a JSON object');
    });

    it('in web/package.json all run under the guard', () => {
      const commands = stylelintCommands(readJson(join(WEB_DIR, 'package.json')));
      expect(commands.length).toBeGreaterThan(0);
      expect(unguardedCommands(commands, prefix)).toEqual([]);
    });
  });

  describe('the committed lint command', () => {
    const command = committedLintCommand();

    it(
      'passes as committed',
      () => {
        expect(runStylelint(command, [])).toEqual({ status: 0, stdout: '', stderr: '' });
      },
      STYLELINT_TEST_TIMEOUT_MS,
    );

    it(
      'lints every stylesheet under src/ while the guard is loaded',
      () => {
        const { status, stdout, stderr } = runStylelint(command, ['--formatter', 'json']);
        expect(status, stderr).toBe(0);
        const results = reportedFiles(stdout.trim() === '' ? stderr : stdout);
        expect(results.map(({ file }) => file).sort()).toEqual(committedStylesheets());
        expect(results.filter(({ warnings }) => warnings > 0)).toEqual([]);
      },
      STYLELINT_TEST_TIMEOUT_MS,
    );

    it.each([
      ['the advisory reproduction', DEEP_PATTERN, 'is 9999 characters long, more than 1000'],
      ['a pattern one group too deep', ONE_TOO_DEEP, 'nests 11 groups deep, more than 10'],
      ['closers hidden in […]', BRACKETED_CLOSERS, 'nests 100 groups deep, more than 10'],
    ])(
      'refuses %s appended as npm run lint -- <glob> appends it',
      (_name, pattern, violation) => {
        const { status, stderr } = runStylelint(command, [pattern]);
        expect(status, stderr).not.toBe(0);
        expect(status, stderr).not.toBeNull();
        expect(stderr).toContain('SyntaxError: Glob pattern ');
        expect(stderr).toContain(`${violation}: refused by the braces depth guard`);
        expect(stderr).toContain(ADVISORY);
        expect(stderr).not.toContain(STACK_OVERFLOW);
      },
      STYLELINT_TEST_TIMEOUT_MS,
    );
  });

  describe('configuration parsing', () => {
    it('collects ignoreFiles, referenceFiles and overrides files, as strings or arrays', () => {
      expect(
        configPatterns(
          {
            ignoreFiles: 'vendor/**',
            referenceFiles: ['tokens.css', { files: ['a/*.css', 'b/*.css'] }],
            overrides: [{ files: 'c/*.css', ignoreFiles: ['d/**'] }, { files: ['e/*.css'] }],
          },
          'cfg',
        ),
      ).toEqual([
        { source: 'cfg ignoreFiles', pattern: 'vendor/**' },
        { source: 'cfg referenceFiles[0]', pattern: 'tokens.css' },
        { source: 'cfg referenceFiles[1].files[0]', pattern: 'a/*.css' },
        { source: 'cfg referenceFiles[1].files[1]', pattern: 'b/*.css' },
        { source: 'cfg overrides[0].files', pattern: 'c/*.css' },
        { source: 'cfg overrides[0] ignoreFiles[0]', pattern: 'd/**' },
        { source: 'cfg overrides[1].files[0]', pattern: 'e/*.css' },
      ]);
      expect(configPatterns({ referenceFiles: { files: 'f/*.css' } }, 'cfg')).toEqual([
        { source: 'cfg referenceFiles[0].files', pattern: 'f/*.css' },
      ]);
    });

    it('rejects pattern properties stylelint would not accept', () => {
      expect(() => configPatterns({ overrides: [{ rules: {} }] }, 'cfg')).toThrow(
        'cfg overrides[0].files is neither a string nor an array of strings',
      );
      expect(() => configPatterns({ ignoreFiles: ['a', 3] }, 'cfg')).toThrow(
        'cfg ignoreFiles[1] is not a string',
      );
      expect(() => configPatterns({ overrides: {} }, 'cfg')).toThrow(
        'cfg overrides is not an array',
      );
    });

    it('judges escapes as written and as stylelint normalises them', () => {
      const depth = MAX_NESTING_DEPTH + 1;
      const escaped = `${'\\{'.repeat(depth)}a${'\\}'.repeat(depth)}`;
      expect(patternViolations(escaped)).toEqual([]);
      expect(describeViolations([{ source: 'cfg ignoreFiles', pattern: escaped }])).toEqual([
        `cfg ignoreFiles: ${JSON.stringify(escaped)} nests 11 groups deep, more than 10`,
      ]);
    });

    it('keeps the committed web/.stylelintrc.json within the bounds', () => {
      const patterns = configPatterns(readJson(STYLELINT_CONFIG_PATH), 'web/.stylelintrc.json');
      expect(patterns.length).toBeGreaterThan(0);
      expect(
        describeViolations(patterns),
        `Every pattern in web/.stylelintrc.json reaches braces 3.0.3 (${ADVISORY}) unguarded when stylelint runs inside the test process, so it must nest at most ${String(MAX_NESTING_DEPTH)} {…}/(…) groups and stay within ${String(MAX_PATTERN_LENGTH)} characters`,
      ).toEqual([]);
    });
  });

  describe('lockfile inventory', () => {
    it('resolves nested installs as Node does and follows every dependency kind', () => {
      const lock = {
        packages: {
          '': { name: 'root', dependencies: { a: '1' }, devDependencies: { b: '1', c: '1' } },
          'node_modules/a': { dependencies: { m: '2' } },
          'node_modules/a/node_modules/m': { optionalDependencies: { braces: '3' } },
          'node_modules/m': { dependencies: {} },
          'node_modules/b': { peerDependencies: { braces: '3' } },
          'node_modules/c': { dependencies: { m: '1' } },
          'node_modules/braces': {},
          'node_modules/c/node_modules/braces': {},
        },
      };
      expect(bracesConsumers(lock)).toEqual({
        installPaths: ['node_modules/braces', 'node_modules/c/node_modules/braces'],
        dependents: ['a', 'b', 'm'],
        rootDependencies: ['a', 'b'],
      });
    });

    it('installs braces once, the instance the guard patches, for stylelint alone', () => {
      const consumers = bracesConsumers(readJson(join(WEB_DIR, 'package-lock.json')));
      const guidance = `braces 3.0.3 (${ADVISORY}) has no fixed release, and the guard patches the one braces instance micromatch loads, in the stylelint process. A second braces install, or a new package that loads braces, escapes it: guard that package's process as well before accepting it. If braces is no longer locked, or the locked release fixes the advisory, delete the guard and this file instead`;
      expect(consumers.installPaths, guidance).toEqual(['node_modules/braces']);
      expect(lockedBracesPath(), guidance).toBe(
        join(WEB_DIR, 'node_modules', 'braces', 'index.js'),
      );
      expect(consumers.dependents, guidance).toEqual([
        'fast-glob',
        'globby',
        'micromatch',
        'stylelint',
      ]);
      expect(consumers.rootDependencies, guidance).toEqual(['stylelint']);
    });
  });
});
