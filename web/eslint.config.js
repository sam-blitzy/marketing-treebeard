// @ts-check
/**
 * ESLint 10 flat configuration for the Ads Action Engine SPA (`web/`).
 *
 * Run by `npm run lint` (`eslint . --max-warnings 0`), which the root Makefile and CI call.
 *
 * What it enforces:
 * - ESLint recommended plus typescript-eslint `strict` (AAP 0.11.2). `strict` is the
 *   non-type-checked variant, so lint needs no TypeScript project service and stays fast;
 *   `npm run typecheck` owns type correctness.
 * - Design-system compliance (AAP 0.4.4): no raw `<button>`, `<input>`, `<select>`,
 *   `<textarea>`, table or heading elements and no `style` attribute outside
 *   `src/design-system/components`. Feature code imports the design system only through
 *   `src/design-system/index.ts` (AAP 0.4.2). Token-only CSS values are stylelint's job
 *   (`.stylelintrc.json`); this file covers TSX.
 * - Server state only through the TanStack Query hooks (AAP 0.11.1): `fetch` and
 *   `XMLHttpRequest` are reachable from `src/api/client.ts` alone, and features never import
 *   that client. Both feature import boundaries cover `import()` as well as static imports.
 * - No build-time configuration in the bundle (AAP 0.3.2): `import.meta.env` is read only for
 *   Vite's built-ins (`BASE_URL`, `DEV`, `MODE`, `PROD`, `SSR`), each by its own name, so no
 *   `VITE_*` value can reach the bundle; `process` is not available in browser code.
 *
 * Flat-config rule options are replaced, not merged: when two config objects set the same
 * rule for one file, the later object's options win outright. Each selector set is therefore
 * defined once below, and every file group passes the full concatenation it needs, so an
 * override can never silently drop a restriction.
 *
 * No React plugin is used: ESLint 10 tracks JSX references natively, which is all
 * `no-unused-vars` needs.
 */
import js from '@eslint/js';
import { defineConfig, globalIgnores } from 'eslint/config';
import tseslint from 'typescript-eslint';

// ---------------------------------------------------------------------------------------------
// Selector sets for `no-restricted-syntax`. Each entry is `{ selector, message }`.
// ---------------------------------------------------------------------------------------------

/**
 * Raw interactive, table and heading elements (AAP 0.4.4: wrappers over raw HTML). Only the
 * design-system wrappers in `src/design-system/components` render these.
 */
const RAW_ELEMENTS = [
  {
    selector:
      'JSXOpeningElement[name.name=/^(button|input|select|textarea|table|thead|tbody|tfoot|tr|th|td|h[1-6])$/]',
    message:
      'Use the design-system wrapper (Button, TextField, Select, DataTable, PageShell heading slots) from src/design-system — raw elements are allowed only in src/design-system/components (AAP 0.4.4).',
  },
];

/**
 * Inline `style` attributes on any element or component (AAP 0.4.4: zero hardcoded values;
 * layout only through Stack, Inline and Grid).
 */
const INLINE_STYLE = [
  {
    selector: "JSXAttribute[name.name='style']",
    message:
      'No literal styles: use design-system wrappers, layout primitives (Stack, Inline, Grid) and var(--…) tokens (AAP 0.4.4).',
  },
];

/**
 * Vite's built-in `import.meta.env` keys (AAP 0.3.2). They describe the build itself and carry
 * no configuration, so they are the only keys browser code may read.
 */
const VITE_BUILT_IN_ENV_KEYS = ['BASE_URL', 'DEV', 'MODE', 'PROD', 'SSR'];

/** esquery regex that matches exactly one of `VITE_BUILT_IN_ENV_KEYS`. */
const VITE_BUILT_IN_ENV_KEY = `/^(${VITE_BUILT_IN_ENV_KEYS.join('|')})$/`;

const VITE_ENV_MESSAGE = `The SPA takes no build-time configuration: import.meta.env is read only for Vite's built-ins ${VITE_BUILT_IN_ENV_KEYS.slice(0, -1).join(', ')} and ${VITE_BUILT_IN_ENV_KEYS.at(-1)}, each by its own name (AAP 0.3.2).`;

const ENV_OBJECT_MESSAGE =
  'Use import.meta and import.meta.env only in a direct read such as import.meta.env.MODE: an alias, spread, argument or computed member hides which variable is read, and Vite inlines the whole env object, every VITE_* value included, wherever it is used as a value (AAP 0.3.2).';

/**
 * Attribute tests that hold when the node at `path` is the dotted `import.meta.env`. `path` is
 * an esquery attribute path relative to the selected node, such as `object` or `init`; an empty
 * path tests the selected node itself.
 *
 * @param {string} path attribute path to the node under test, or `''` for the node itself
 * @returns {string} the concatenated esquery attribute selectors
 */
function importMetaEnvAt(path) {
  const at = path === '' ? '' : `${path}.`;
  return [
    `[${at}type='MemberExpression']`,
    `[${at}computed=false]`,
    `[${at}object.type='MetaProperty']`,
    `[${at}object.meta.name='import']`,
    `[${at}property.name='env']`,
  ].join('');
}

/** An object pattern that destructures `import.meta.env` in a declaration. */
const ENV_PATTERN = `VariableDeclarator[id.type='ObjectPattern']${importMetaEnvAt('init')} > ObjectPattern.id`;

/**
 * `import.meta.env` reads (AAP 0.3.2: no `VITE_` variable carries a secret, and the SPA needs no
 * build-time configuration). The restriction is an allowlist, not a list of forbidden names:
 * only `VITE_BUILT_IN_ENV_KEYS` may be read, each by its own name, as `import.meta.env.MODE`,
 * `import.meta.env?.MODE`, `import.meta.env['MODE']` or `const { MODE } = import.meta.env`.
 * Every other read is reported once, whatever its key or syntax:
 * - a computed member of `import.meta`, such as `import.meta['env']`;
 * - `import.meta` used as a value rather than as the object of a dotted member, such as an
 *   alias (`import.meta.url`, `import.meta.hot` and `new.target` stay allowed);
 * - `import.meta.env` used as a value: an alias, spread, call argument, computed key of another
 *   object, TypeScript `as`, `!` or `satisfies` wrapper, or destructuring assignment. Vite
 *   inlines the whole env object wherever it is used this way, so every `VITE_*` value would
 *   reach the bundle;
 * - a dotted, string-literal-computed or destructured key outside the allowlist;
 * - a computed key that is not a string literal (template literal, identifier or expression),
 *   and a destructuring rest element, whose keys cannot be checked.
 */
const VITE_ENV = [
  {
    selector:
      "MemberExpression[computed=true][object.type='MetaProperty'][object.meta.name='import']",
    message: ENV_OBJECT_MESSAGE,
  },
  {
    selector: "MetaProperty[meta.name='import']:not(MemberExpression > MetaProperty.object)",
    message: ENV_OBJECT_MESSAGE,
  },
  {
    selector: `${importMetaEnvAt('')}:not(MemberExpression > MemberExpression.object, VariableDeclarator[id.type='ObjectPattern'] > MemberExpression.init)`,
    message: ENV_OBJECT_MESSAGE,
  },
  {
    selector: `MemberExpression[computed=false]${importMetaEnvAt('object')}[property.name!=${VITE_BUILT_IN_ENV_KEY}]`,
    message: VITE_ENV_MESSAGE,
  },
  {
    selector: `MemberExpression[computed=true]${importMetaEnvAt('object')}[property.type='Literal'][property.value!=${VITE_BUILT_IN_ENV_KEY}]`,
    message: VITE_ENV_MESSAGE,
  },
  {
    selector: `MemberExpression[computed=true]${importMetaEnvAt('object')}[property.type!='Literal']`,
    message: VITE_ENV_MESSAGE,
  },
  { selector: `${ENV_PATTERN} > RestElement`, message: VITE_ENV_MESSAGE },
  {
    selector: `${ENV_PATTERN} > Property[computed=false][key.type='Identifier'][key.name!=${VITE_BUILT_IN_ENV_KEY}]`,
    message: VITE_ENV_MESSAGE,
  },
  {
    selector: `${ENV_PATTERN} > Property[key.type='Literal'][key.value!=${VITE_BUILT_IN_ENV_KEY}]`,
    message: VITE_ENV_MESSAGE,
  },
  {
    selector: `${ENV_PATTERN} > Property[computed=true][key.type!='Literal']`,
    message: VITE_ENV_MESSAGE,
  },
];

/** Everything application code outside the design-system internals may not contain. */
const APP_SYNTAX = [...RAW_ELEMENTS, ...INLINE_STYLE, ...VITE_ENV];

// ---------------------------------------------------------------------------------------------
// Restricted globals for `no-restricted-globals`.
// ---------------------------------------------------------------------------------------------

const DIRECT_HTTP_MESSAGE =
  'Use the TanStack Query hooks in src/api/queries.ts; only src/api/client.ts calls fetch (AAP 0.11.1).';

/** Direct HTTP from components (AAP 0.11.1). Allowed only in `src/api/client.ts`. */
const NO_DIRECT_HTTP = [
  { name: 'fetch', message: DIRECT_HTTP_MESSAGE },
  { name: 'XMLHttpRequest', message: DIRECT_HTTP_MESSAGE },
];

/** Node's `process`, which does not exist in the browser bundle (AAP 0.3.2). */
const NO_PROCESS = { name: 'process', message: 'process is not available in the browser bundle.' };

const RESTRICTED_GLOBALS = [...NO_DIRECT_HTTP, NO_PROCESS];

/**
 * Builds the `no-restricted-globals` options for a list of restricted names.
 *
 * `checkGlobalObject` also reports property reads through the global objects, such as
 * `window.fetch(...)` or `globalThis.process`, so the restriction covers more than the bare
 * identifier. The rule only sees global objects declared in scope, which is why the app-source
 * block declares `window` and `self` (`globalThis` is an ES2020 built-in that ESLint always
 * declares).
 *
 * @param {Array<{ name: string, message: string }>} globals restricted names with messages
 * @returns {['error', { globals: Array<{ name: string, message: string }>, checkGlobalObject: true }]}
 */
function restrictGlobals(globals) {
  return ['error', { globals, checkGlobalObject: true }];
}

/**
 * Browser global objects declared for `src/`, so `checkGlobalObject` can follow
 * `window.fetch` and `self.fetch`. TypeScript files do not use `no-undef` (typescript-eslint's
 * `eslint-recommended` layer turns it off), so this list only feeds that check.
 */
const BROWSER_GLOBAL_OBJECTS = {
  window: 'readonly',
  self: 'readonly',
};

/**
 * Node.js globals for the files that run in Node: Playwright specs, the Vite and Playwright
 * configs and this file. The `globals` package is not a dependency of `web/package.json`, so
 * the few names these files may reference are declared here. Only `eslint.config.js` is
 * checked by `no-undef`; TypeScript files rely on `@types/node` through `tsconfig.json`.
 */
const NODE_GLOBALS = {
  process: 'readonly',
  console: 'readonly',
  Buffer: 'readonly',
  URL: 'readonly',
  URLSearchParams: 'readonly',
  fetch: 'readonly',
  AbortController: 'readonly',
  AbortSignal: 'readonly',
  structuredClone: 'readonly',
  setTimeout: 'readonly',
  clearTimeout: 'readonly',
  setInterval: 'readonly',
  clearInterval: 'readonly',
};

// ---------------------------------------------------------------------------------------------
// Import boundaries for feature code (`no-restricted-imports` for static imports,
// `no-restricted-syntax` for `import()`).
// ---------------------------------------------------------------------------------------------

const DESIGN_SYSTEM_IMPORT_MESSAGE =
  'Import from the design-system index (src/design-system/index.ts) only (AAP 0.4.2).';

const API_CLIENT_IMPORT_MESSAGE =
  'Features use the hooks and re-exported types in src/api/queries.ts and src/api/types.ts, never apiFetch directly.';

const UNCHECKED_IMPORT_MESSAGE =
  'Feature code imports modules by string-literal specifiers only, never by a computed import() or import.meta.glob, so the design-system index (AAP 0.4.2) and Query-hook (AAP 0.11.1) boundaries can be checked.';

/**
 * Regex source, without delimiters or flags, for a specifier that names a module under the
 * design system other than its index: components, tokens.ts, tokens.css and anything added
 * later, at any relative depth such as `../../design-system/components/Button`. `index` and
 * `index.<ext>` stay allowed, and so do the bare `design-system` and `design-system/`
 * specifiers, which resolve to the index. Repeated slashes and `.` segments, which resolve the
 * same, are tolerated, and a `..` segment leaves the design system.
 *
 * Only the path portion is matched: everything before the first `?` or `#`. A `?query` or
 * `#fragment` suffix never changes which module a specifier names, so
 * `design-system/components/Button?raw` still matches, while `design-system/index.ts?x=/` and a
 * `design-system/components` that appears only inside the suffix of another module do not.
 *
 * This is the one definition of the design-system boundary: static imports
 * (`FEATURE_IMPORT_PATTERNS`) and `import()` (`FEATURE_DYNAMIC_IMPORTS`) both compile it with the
 * `iu` flags, so they match exactly the same specifiers, case-insensitively. Every `/` is
 * escaped, which the `u` flag accepts and an esquery regex literal requires.
 */
const DESIGN_SYSTEM_INTERNAL_SOURCE = String.raw`^([^?#]*\/)?design-system(\/+\.(?=[\/?#]|$))*\/+(?!\.{1,2}([\/?#]|$)|index(\.[^\/?#]*)?([?#]|$))[^\/?#]`;

/**
 * Regex source, without delimiters or flags, for a specifier that enters an `api` directory and
 * ends in a `client` module, with or without an extension. Any path between `api/` and `client`
 * is tolerated, so empty and `.` segments and `..` detours such as `api/queries/../client`,
 * which resolve to the same module, all match; so does a nested `api/<dir>/client`, which is
 * still the API client by name. Other modules such as `clientele` or `client-types`, and
 * `queries` or `types`, do not match.
 *
 * Only the path portion is matched: everything before the first `?` or `#`. A `?query` or
 * `#fragment` suffix, whatever it contains, never changes which module a specifier names, so
 * `api/client#x` and `api/client.ts?x=/` match, while `api/queries?x=/api/client` does not.
 *
 * This is the one definition of the API-client boundary, shared by static imports and
 * `import()` with the same `iu` flags and escaping as `DESIGN_SYSTEM_INTERNAL_SOURCE`.
 */
const API_CLIENT_SOURCE = String.raw`^([^?#]*\/)?api\/([^?#]*\/)?client(\.[^\/?#]*)?([?#].*)?$`;

/**
 * The feature import boundaries for static imports, `import type` and re-exports
 * (`export … from`), checked by `no-restricted-imports`. Each entry is a `regex` built from a
 * shared boundary source above; `caseSensitive` is unset, so the rule compiles it with the `iu`
 * flags, as `FEATURE_DYNAMIC_IMPORTS` does, and reports the same message.
 */
const FEATURE_IMPORT_PATTERNS = [
  { regex: DESIGN_SYSTEM_INTERNAL_SOURCE, message: DESIGN_SYSTEM_IMPORT_MESSAGE },
  { regex: API_CLIENT_SOURCE, message: API_CLIENT_IMPORT_MESSAGE },
];

/**
 * Builds the `no-restricted-syntax` selector for an `import()` expression or `import('…')` type
 * whose string-literal specifier matches a shared boundary regex source. The esquery regex
 * literal carries the `iu` flags `no-restricted-imports` compiles the same source with, so
 * `import()` and static imports match exactly the same specifiers.
 *
 * @param {string} source `DESIGN_SYSTEM_INTERNAL_SOURCE` or `API_CLIENT_SOURCE`
 * @returns {string} the esquery selector
 */
function dynamicImportOf(source) {
  return `:matches(ImportExpression, TSImportType)[source.type='Literal'][source.value=/${source}/iu]`;
}

/**
 * The feature import boundaries for `import()` (AAP 0.4.2: features import the design system
 * only through its index; AAP 0.11.1: server state only through the TanStack Query hooks).
 * `no-restricted-imports` sees static imports and re-exports only, so these selectors apply the
 * same two boundaries, built by `dynamicImportOf` from the regex sources `FEATURE_IMPORT_PATTERNS`
 * uses, to `import()` expressions and to type-position `import('…')` types, which the static
 * rule already covers for `import type`. An `import()` whose specifier is not a string literal,
 * such as a template literal that Vite expands at build time, and `import.meta.glob` cannot be
 * checked against either boundary and are reported as well. Lazy loading by literal specifier,
 * such as `lazy(() => import('./ActionsPage'))`, stays allowed.
 */
const FEATURE_DYNAMIC_IMPORTS = [
  {
    selector: dynamicImportOf(DESIGN_SYSTEM_INTERNAL_SOURCE),
    message: DESIGN_SYSTEM_IMPORT_MESSAGE,
  },
  {
    selector: dynamicImportOf(API_CLIENT_SOURCE),
    message: API_CLIENT_IMPORT_MESSAGE,
  },
  {
    selector: "ImportExpression[source.type!='Literal']",
    message: UNCHECKED_IMPORT_MESSAGE,
  },
  {
    selector:
      "MemberExpression[object.type='MetaProperty'][object.meta.name='import'][property.name='glob']",
    message: UNCHECKED_IMPORT_MESSAGE,
  },
];

export default defineConfig([
  // 1. Generated output is never linted (build, coverage and Playwright/Vitest artefacts).
  //    node_modules/ is ignored by ESLint itself. playwright/.cache/ mirrors the root
  //    .gitignore entry for Playwright's own cache, which ESLint would otherwise lint because
  //    flat config does not skip dot-directories.
  globalIgnores(
    ['dist/', 'coverage/', 'playwright-report/', 'test-results/', '.vitest/', 'playwright/.cache/'],
    'web/generated-output',
  ),

  // 2. ESLint recommended for every linted file (AAP 0.11.2).
  {
    name: 'web/javascript-recommended',
    extends: [js.configs.recommended],
  },

  // 3. typescript-eslint strict, non-type-checked, for all TypeScript (AAP 0.11.2).
  {
    name: 'web/typescript-strict',
    files: ['**/*.{ts,tsx}'],
    extends: [tseslint.configs.strict],
  },

  // 4. App source: design-system compliance (AAP 0.4.4), no direct HTTP (AAP 0.11.1) and no
  //    build-time configuration or Node globals in the bundle (AAP 0.3.2).
  {
    name: 'web/app-source',
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      globals: BROWSER_GLOBAL_OBJECTS,
    },
    rules: {
      'no-restricted-syntax': ['error', ...APP_SYNTAX],
      'no-restricted-globals': restrictGlobals(RESTRICTED_GLOBALS),
    },
  },

  // 5. Feature code: the full syntax set again (options replace, never merge), plus the import
  //    boundaries: design system through its index only (AAP 0.4.2), server state through
  //    src/api/queries.ts only (AAP 0.11.1). Static imports go through no-restricted-imports,
  //    import() and import('…') types through FEATURE_DYNAMIC_IMPORTS. Feature unit tests
  //    match block 8 as well, which replaces both rules, so neither boundary applies to them.
  {
    name: 'web/features',
    files: ['src/features/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...APP_SYNTAX, ...FEATURE_DYNAMIC_IMPORTS],
      'no-restricted-imports': ['error', { patterns: FEATURE_IMPORT_PATTERNS }],
    },
  },

  // 6. Design-system internals: the one place raw elements and `style` are allowed
  //    (AAP 0.4.4). The import.meta.env allowlist still applies, and no-restricted-globals from
  //    block 4 is left untouched, so fetch, XMLHttpRequest and process stay forbidden here.
  {
    name: 'web/design-system-components',
    files: ['src/design-system/components/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...VITE_ENV],
    },
  },

  // 7. The one fetch site (AAP 0.11.1): src/api/client.ts may call fetch; process stays
  //    forbidden (AAP 0.3.2).
  {
    name: 'web/api-client',
    files: ['src/api/client.ts'],
    rules: {
      'no-restricted-globals': restrictGlobals([NO_PROCESS]),
    },
  },

  // 8. Unit tests: they stub fetch and may import the client to build errors, so the globals
  //    and import restrictions are off. Tests still render only through the design-system
  //    wrappers (AAP 0.4.4) and read import.meta.env only for Vite's built-ins (AAP 0.3.2).
  {
    name: 'web/unit-tests',
    files: ['src/**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...APP_SYNTAX],
      'no-restricted-globals': 'off',
      'no-restricted-imports': 'off',
    },
  },
  //    Tests of the design-system internals follow block 6: only the import.meta.env allowlist
  //    applies.
  {
    name: 'web/design-system-component-tests',
    files: ['src/design-system/components/**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...VITE_ENV],
    },
  },

  // 9. Node-side files (Playwright specs, tool configs, this file) run in Node, not in the
  //    browser bundle: Node globals are declared and process is allowed.
  {
    name: 'web/node-side',
    files: ['tests/**/*.ts', 'vite.config.ts', 'playwright.config.ts', 'eslint.config.js'],
    languageOptions: {
      globals: NODE_GLOBALS,
    },
    rules: {
      'no-restricted-globals': 'off',
    },
  },
]);
