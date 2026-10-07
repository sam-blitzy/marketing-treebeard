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
 *   that client.
 * - No build-time configuration in the bundle (AAP 0.3.2): `import.meta.env.VITE_*` is never
 *   read, and `process` is not available in browser code.
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

const VITE_ENV_MESSAGE =
  'The SPA takes no build-time configuration; never read VITE_* variables (AAP 0.3.2).';

/**
 * Reads of `VITE_*` build-time variables (AAP 0.3.2: no `VITE_` variable carries a secret, and
 * the SPA needs no build-time configuration). The first selector is the dotted read
 * `import.meta.env.VITE_X`. The second and third close the two other ways to reach the same
 * value, `import.meta.env['VITE_X']` and `const { VITE_X } = import.meta.env`, so the
 * restriction cannot be sidestepped by syntax alone.
 */
const VITE_ENV = [
  {
    selector:
      "MemberExpression[object.type='MemberExpression'][object.object.type='MetaProperty'][object.property.name='env'][property.name=/^VITE_/]",
    message: VITE_ENV_MESSAGE,
  },
  {
    selector:
      "MemberExpression[computed=true][object.type='MemberExpression'][object.object.type='MetaProperty'][object.property.name='env'][property.value=/^VITE_/]",
    message: VITE_ENV_MESSAGE,
  },
  {
    selector:
      "VariableDeclarator[init.type='MemberExpression'][init.object.type='MetaProperty'][init.property.name='env'] > ObjectPattern > Property[key.name=/^VITE_/]",
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
// Import boundaries for feature code (`no-restricted-imports`).
// ---------------------------------------------------------------------------------------------

/**
 * Patterns are gitignore-style and match relative specifiers such as
 * `../../design-system/components/Button`. `design-system/*` restricts every module under the
 * design system (components, tokens.ts, tokens.css and anything added later); the negations
 * re-allow its index, with or without an extension, and the bare `../../design-system`
 * specifier never matches. `client.*` also covers an explicit `.js` or `.ts` extension on the
 * client import.
 */
const FEATURE_IMPORT_PATTERNS = [
  {
    group: ['**/design-system/*', '!**/design-system/index', '!**/design-system/index.*'],
    message: 'Import from the design-system index (src/design-system/index.ts) only (AAP 0.4.2).',
  },
  {
    group: ['**/api/client', '**/api/client.*'],
    message:
      'Features use the hooks and re-exported types in src/api/queries.ts and src/api/types.ts, never apiFetch directly.',
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
  //    src/api/queries.ts only (AAP 0.11.1).
  {
    name: 'web/features',
    files: ['src/features/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...APP_SYNTAX],
      'no-restricted-imports': ['error', { patterns: FEATURE_IMPORT_PATTERNS }],
    },
  },

  // 6. Design-system internals: the one place raw elements and `style` are allowed
  //    (AAP 0.4.4). The VITE_* ban still applies, and no-restricted-globals from block 4 is
  //    left untouched, so fetch, XMLHttpRequest and process stay forbidden here.
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
  //    wrappers (AAP 0.4.4) and never read VITE_* variables (AAP 0.3.2).
  {
    name: 'web/unit-tests',
    files: ['src/**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', ...APP_SYNTAX],
      'no-restricted-globals': 'off',
      'no-restricted-imports': 'off',
    },
  },
  //    Tests of the design-system internals follow block 6: only the VITE_* ban applies.
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
