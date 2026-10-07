// @vitest-environment node
/**
 * Behaviour tests for web/eslint.config.js.
 *
 * Each restriction is exercised through ESLint's Node API on virtual files: `lintText` takes the
 * code and a path relative to web/, and ESLint resolves the real flat config for that path, so
 * these tests see exactly what `npm run lint` sees. Every case asserts the complete list of
 * reports (rule id, severity, line and message), which proves both that a restriction fires
 * where it must and that nothing else fires where the config allows the construct.
 */
import { fileURLToPath } from 'node:url';

import { ESLint } from 'eslint';
import { beforeAll, describe, expect, it } from 'vitest';

/**
 * The web/ package root, which holds eslint.config.js. Derived from this file's URL, never from
 * process.
 */
const WEB_DIR = fileURLToPath(new URL('..', import.meta.url));

const eslint = new ESLint({ cwd: WEB_DIR });

/** ESLint's numeric severity for `'error'`. */
const ERROR = 2;

const RAW_ELEMENT_MESSAGE =
  'Use the design-system wrapper (Button, TextField, Select, DataTable, PageShell heading slots) from src/design-system — raw elements are allowed only in src/design-system/components (AAP 0.4.4).';
const INLINE_STYLE_MESSAGE =
  'No literal styles: use design-system wrappers, layout primitives (Stack, Inline, Grid) and var(--…) tokens (AAP 0.4.4).';
const VITE_ENV_MESSAGE =
  "The SPA takes no build-time configuration: import.meta.env is read only for Vite's built-ins BASE_URL, DEV, MODE, PROD and SSR, each by its own name (AAP 0.3.2).";
const ENV_OBJECT_MESSAGE =
  'Use import.meta and import.meta.env only in a direct read such as import.meta.env.MODE: an alias, spread, argument or computed member hides which variable is read, and Vite inlines the whole env object, every VITE_* value included, wherever it is used as a value (AAP 0.3.2).';
const DIRECT_HTTP_MESSAGE =
  'Use the TanStack Query hooks in src/api/queries.ts; only src/api/client.ts calls fetch (AAP 0.11.1).';
const PROCESS_MESSAGE = 'process is not available in the browser bundle.';
const DESIGN_SYSTEM_IMPORT_MESSAGE =
  'Import from the design-system index (src/design-system/index.ts) only (AAP 0.4.2).';
const API_CLIENT_IMPORT_MESSAGE =
  'Features use the hooks and re-exported types in src/api/queries.ts and src/api/types.ts, never apiFetch directly.';
const UNCHECKED_IMPORT_MESSAGE =
  'Feature code imports modules by string-literal specifiers only, never by a computed import() or import.meta.glob, so the design-system index (AAP 0.4.2) and Query-hook (AAP 0.11.1) boundaries can be checked.';

/** Every element name the raw-element selector matches. */
const RAW_ELEMENTS = [
  'button',
  'input',
  'select',
  'textarea',
  'table',
  'thead',
  'tbody',
  'tfoot',
  'tr',
  'th',
  'td',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
] as const;

const FEATURE_TSX = 'src/features/actions/Example.tsx';
const FEATURE_TS = 'src/features/actions/example.ts';
const DESIGN_SYSTEM_COMPONENT = 'src/design-system/components/Button.tsx';
const API_CLIENT = 'src/api/client.ts';
const API_QUERIES = 'src/api/queries.ts';

/** One ESLint report, reduced to the fields these tests assert. */
interface Report {
  readonly ruleId: string | null;
  readonly severity: number;
  readonly line: number;
  readonly message: string;
}

/**
 * Lints `codeLines` (joined with newlines) as the virtual file `filePath`, relative to web/.
 *
 * `warnIgnored` turns an ignored path into a reported warning, so an "allowed" case can never
 * pass because the file fell outside the config.
 */
async function lint(filePath: string, ...codeLines: readonly string[]): Promise<Report[]> {
  const results = await eslint.lintText(codeLines.join('\n'), { filePath, warnIgnored: true });
  const [result] = results;
  if (result === undefined || results.length !== 1) {
    throw new Error(`ESLint returned ${String(results.length)} results for ${filePath}`);
  }
  return result.messages.map(({ ruleId, severity, line, message }) => ({
    ruleId,
    severity,
    line,
    message,
  }));
}

/** The report `no-restricted-syntax` produces for `message` on `line`. */
function syntaxReport(line: number, message: string): Report {
  return { ruleId: 'no-restricted-syntax', severity: ERROR, line, message };
}

/** The report `no-restricted-globals` produces for `name` on `line`. */
function globalReport(line: number, name: 'fetch' | 'XMLHttpRequest' | 'process'): Report {
  const detail = name === 'process' ? PROCESS_MESSAGE : DIRECT_HTTP_MESSAGE;
  return {
    ruleId: 'no-restricted-globals',
    severity: ERROR,
    line,
    message: `Unexpected use of '${name}'. ${detail}`,
  };
}

/** The report `no-restricted-imports` produces for the import of `source` on `line`. */
function importReport(line: number, source: string, message: string): Report {
  return {
    ruleId: 'no-restricted-imports',
    severity: ERROR,
    line,
    message: `'${source}' import is restricted from being used by a pattern. ${message}`,
  };
}

beforeAll(async () => {
  // Loading the flat config and the TypeScript parser takes seconds on a busy host; do it once,
  // outside the per-test timeout.
  await lint('src/warm-up.tsx', 'export const WarmUp = () => <div />;');
}, 120_000);

describe('raw elements (AAP 0.4.4)', () => {
  it.each(RAW_ELEMENTS)('reports <%s> in feature code', async (element) => {
    expect(await lint(FEATURE_TSX, `export const View = () => <${element} />;`)).toEqual([
      syntaxReport(1, RAW_ELEMENT_MESSAGE),
    ]);
  });

  it.each([
    'src/router.tsx',
    'src/design-system/Layout.tsx',
    'src/features/actions/sections/KeywordsSection.tsx',
    'src/features/actions/ActionsPage.test.tsx',
  ])('reports raw elements in %s, outside src/design-system/components', async (filePath) => {
    expect(
      await lint(
        filePath,
        'export const View = () => (',
        '  <div>',
        '    <h2>Keywords to adjust</h2>',
        '    <button type="button">Dismiss</button>',
        '  </div>',
        ');',
      ),
    ).toEqual([syntaxReport(3, RAW_ELEMENT_MESSAGE), syntaxReport(4, RAW_ELEMENT_MESSAGE)]);
  });

  it('allows wrappers and neutral elements in feature code', async () => {
    expect(
      await lint(
        FEATURE_TSX,
        "import { Button, DataTable, Select, TextField } from '../../design-system';",
        'export const View = () => (',
        '  <section>',
        '    <header>',
        '      <p>',
        '        <a href="#keywords">',
        '          <span>Keywords</span>',
        '        </a>',
        '      </p>',
        '    </header>',
        '    <Button />',
        '    <TextField />',
        '    <Select />',
        '    <DataTable />',
        '  </section>',
        ');',
      ),
    ).toEqual([]);
  });

  it.each([
    DESIGN_SYSTEM_COMPONENT,
    'src/design-system/components/DataTable/DataTable.tsx',
    'src/design-system/components/Button.test.tsx',
  ])('allows every raw element and the style attribute in %s', async (filePath) => {
    const elements = RAW_ELEMENTS.map((element) => `<${element} />`).join('');
    expect(
      await lint(
        filePath,
        `export const Raw = () => <div style={{ inlineSize: 'auto' }}>${elements}</div>;`,
      ),
    ).toEqual([]);
  });
});

describe('the style attribute (AAP 0.4.4)', () => {
  it.each([FEATURE_TSX, 'src/router.tsx', 'src/features/actions/ActionsPage.test.tsx'])(
    'reports style on elements and on wrapper components in %s',
    async (filePath) => {
      expect(
        await lint(
          filePath,
          "import { Stack } from '../../design-system';",
          "export const A = () => <div style={{ color: 'red' }} />;",
          'export const B = () => <Stack style={{ gap: 0 }} />;',
        ),
      ).toEqual([syntaxReport(2, INLINE_STYLE_MESSAGE), syntaxReport(3, INLINE_STYLE_MESSAGE)]);
    },
  );
});

describe('VITE_* reads (AAP 0.3.2)', () => {
  /** One file from every src/ config block that carries the import.meta.env allowlist. */
  const ENV_READ_FILES = [
    FEATURE_TS,
    API_CLIENT,
    API_QUERIES,
    DESIGN_SYSTEM_COMPONENT,
    'src/features/actions/example.test.ts',
    'src/design-system/components/Button.test.tsx',
  ] as const;

  it.each(ENV_READ_FILES)(
    'reports the dotted, computed and destructured reads in %s',
    async (filePath) => {
      expect(
        await lint(
          filePath,
          'export const dotted = import.meta.env.VITE_API_URL;',
          "export const computed = import.meta.env['VITE_API_URL'];",
          'const { VITE_API_URL } = import.meta.env;',
          'export const destructured = VITE_API_URL;',
        ),
      ).toEqual([
        syntaxReport(1, VITE_ENV_MESSAGE),
        syntaxReport(2, VITE_ENV_MESSAGE),
        syntaxReport(3, VITE_ENV_MESSAGE),
      ]);
    },
  );

  it.each([FEATURE_TS, API_CLIENT, DESIGN_SYSTEM_COMPONENT])(
    "allows Vite's built-in variables in %s",
    async (filePath) => {
      expect(
        await lint(
          filePath,
          'export const mode = import.meta.env.MODE;',
          "export const dev = import.meta.env['DEV'];",
          'const { BASE_URL } = import.meta.env;',
          'export const base = BASE_URL;',
        ),
      ).toEqual([]);
    },
  );

  it.each(ENV_READ_FILES)(
    'reports computed, aliased and non-built-in reads of import.meta.env in %s',
    async (filePath) => {
      expect(
        await lint(
          filePath,
          "export const computedEnv = import.meta['env'].VITE_SECRET;",
          "const { ['VITE_SECRET']: value } = import.meta.env;",
          "const { 'VITE_X': quoted } = import.meta.env;",
          "export const template = import.meta.env[`VITE_${'X'}`];",
          'export const plainTemplate = import.meta.env[`VITE_X`];',
          "const key = 'VITE_X';",
          'export const byIdentifier = import.meta.env[key];',
          'const { ...rest } = import.meta.env;',
          'const env = import.meta.env;',
          'const meta = import.meta;',
          'export const spread = { ...import.meta.env };',
          'export const passed = String(import.meta.env);',
          'export const asserted = (import.meta.env as Record<string, string>).VITE_X;',
          'export const secret = import.meta.env.SECRET_TOKEN;',
          'export const used = [value, quoted, rest, env.VITE_X, meta.env.VITE_X];',
        ),
      ).toEqual([
        syntaxReport(1, ENV_OBJECT_MESSAGE),
        syntaxReport(2, VITE_ENV_MESSAGE),
        syntaxReport(3, VITE_ENV_MESSAGE),
        syntaxReport(4, VITE_ENV_MESSAGE),
        syntaxReport(5, VITE_ENV_MESSAGE),
        syntaxReport(7, VITE_ENV_MESSAGE),
        syntaxReport(8, VITE_ENV_MESSAGE),
        syntaxReport(9, ENV_OBJECT_MESSAGE),
        syntaxReport(10, ENV_OBJECT_MESSAGE),
        syntaxReport(11, ENV_OBJECT_MESSAGE),
        syntaxReport(12, ENV_OBJECT_MESSAGE),
        syntaxReport(13, ENV_OBJECT_MESSAGE),
        syntaxReport(14, VITE_ENV_MESSAGE),
      ]);
    },
  );

  it.each(ENV_READ_FILES)(
    'allows every built-in, import.meta.url and new.target in %s',
    async (filePath) => {
      expect(
        await lint(
          filePath,
          'export const builtIns = [',
          '  import.meta.env.BASE_URL,',
          '  import.meta.env.DEV,',
          '  import.meta.env.MODE,',
          '  import.meta.env.PROD,',
          '  import.meta.env.SSR,',
          '];',
          "export const dev = import.meta.env['DEV'];",
          'const { BASE_URL, MODE } = import.meta.env;',
          'export const destructured = [BASE_URL, MODE];',
          'export const moduleUrl = import.meta.url;',
          'export const optionalMode = import.meta.env?.MODE;',
          'export class Base {',
          '  readonly constructedAs: unknown;',
          '  constructor() {',
          '    this.constructedAs = new.target;',
          '  }',
          '}',
        ),
      ).toEqual([]);
    },
  );
});

describe('direct HTTP (AAP 0.11.1)', () => {
  const directHttp = [
    "export const viaFetch = fetch('/api/v1/healthz');",
    'export const viaXhr = new XMLHttpRequest();',
    "export const viaWindow = window.fetch('/api/v1/healthz');",
    "export const viaSelf = self.fetch('/api/v1/healthz');",
    "export const viaGlobalThis = globalThis.fetch('/api/v1/healthz');",
  ] as const;

  it.each([
    FEATURE_TS,
    API_QUERIES,
    DESIGN_SYSTEM_COMPONENT,
    'src/lib/format.ts',
    'src/router.tsx',
  ])(
    'reports fetch and XMLHttpRequest, bare and through global objects, in %s',
    async (filePath) => {
      expect(await lint(filePath, ...directHttp)).toEqual([
        globalReport(1, 'fetch'),
        globalReport(2, 'XMLHttpRequest'),
        globalReport(3, 'fetch'),
        globalReport(4, 'fetch'),
        globalReport(5, 'fetch'),
      ]);
    },
  );

  it.each([API_CLIENT, 'src/api/client.test.ts', 'src/features/actions/ActionsPage.test.tsx'])(
    'allows fetch and XMLHttpRequest in %s',
    async (filePath) => {
      expect(await lint(filePath, ...directHttp)).toEqual([]);
    },
  );
});

describe('process (AAP 0.3.2)', () => {
  const processReads = [
    'export const nodeEnv = process.env.NODE_ENV;',
    'export const viaGlobalThis = globalThis.process;',
  ] as const;

  it.each([FEATURE_TS, API_CLIENT, API_QUERIES, DESIGN_SYSTEM_COMPONENT, 'src/main.tsx'])(
    'reports process in browser code: %s',
    async (filePath) => {
      expect(await lint(filePath, ...processReads)).toEqual([
        globalReport(1, 'process'),
        globalReport(2, 'process'),
      ]);
    },
  );

  it.each([
    'tests/e2e/actions.spec.ts',
    'vite.config.ts',
    'playwright.config.ts',
    'eslint.config.js',
  ])(
    'allows process in the Node-side file %s',
    async (filePath) => {
      expect(await lint(filePath, ...processReads)).toEqual([]);
    },
  );
});

describe('feature import boundaries (AAP 0.4.2, AAP 0.11.1)', () => {
  /**
   * Specifiers that reach a restricted module through an equivalent path (a repeated slash, a
   * `.` segment or a `..` detour) or in another letter case. Static imports and import() share
   * one definition per boundary, so both must report every one of them.
   */
  const EQUIVALENT_SPECIFIERS = [
    ['../../design-system//components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../design-system/./components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../Design-System/components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../design-system/../design-system/components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../api//client', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/./client', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/Client', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/queries/../client', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/x/../client', API_CLIENT_IMPORT_MESSAGE],
  ] as const;

  /**
   * Specifiers whose path portion, everything before the first `?` or `#`, names a restricted
   * module. The suffix never changes which module is named, whatever it contains, so static
   * imports and import() must report every one of them.
   */
  const SUFFIXED_SPECIFIERS = [
    ['../../api/client#fragment', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/client?probe=/', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/client.ts?probe=/', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/client.ts?raw', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/client?x#y', API_CLIENT_IMPORT_MESSAGE],
    ['../../design-system/components/Button?probe=/', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../design-system/tokens.css#x', DESIGN_SYSTEM_IMPORT_MESSAGE],
  ] as const;

  /**
   * Specifiers that name a design-system internal without a relative prefix: root-relative,
   * which Vite resolves from web/, and bare. The boundary matches the `design-system` directory
   * as a whole path segment wherever the path starts, so neither escapes it.
   */
  const UNPREFIXED_SPECIFIERS = [
    ['/src/design-system/components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['design-system/components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE],
  ] as const;

  /**
   * Specifiers whose path portion names an allowed module: the design-system index, the query
   * layer, a `..` that leaves the client, or a module that only shares a prefix or a directory
   * name with a restricted one. A restricted name inside the `?query` suffix does not make them
   * restricted.
   */
  const UNRESTRICTED_SPECIFIERS = [
    '../../design-system?x=/components/Button',
    '../../design-system/index?x=/',
    '../../design-system/index.ts?x=/',
    '../../design-system/index.ts#f',
    '../../design-system/?x',
    '../../api/queries?x=/api/client',
    '../../lib/format?x=/design-system/components',
    '../../api/clientele',
    '../../api/client-types',
    '../../myapi/client',
    '../../my-design-system/components',
    '../../api/client/../queries',
  ] as const;

  it.each([
    '../../design-system/components',
    '../../design-system/components/Button',
    '../../design-system/components/Button.tsx',
    '../../design-system/tokens',
    '../../design-system/tokens.ts',
    '../../design-system/tokens.css',
  ])('reports the design-system internal %s', async (source) => {
    expect(await lint(FEATURE_TS, `import '${source}';`)).toEqual([
      importReport(1, source, DESIGN_SYSTEM_IMPORT_MESSAGE),
    ]);
  });

  it.each(['../../api/client', '../../api/client.ts', '../../api/client.js'])(
    'reports the API client %s',
    async (source) => {
      expect(await lint(FEATURE_TS, `import '${source}';`)).toEqual([
        importReport(1, source, API_CLIENT_IMPORT_MESSAGE),
      ]);
    },
  );

  it('reports type-only imports of the API client', async () => {
    expect(
      await lint(
        FEATURE_TS,
        "import type { ApiError } from '../../api/client';",
        'export type { ApiError };',
      ),
    ).toEqual([importReport(1, '../../api/client', API_CLIENT_IMPORT_MESSAGE)]);
  });

  it('reports internals at any feature depth', async () => {
    expect(
      await lint(
        'src/features/actions/sections/KeywordsSection.tsx',
        "import '../../../design-system/components/Button';",
        "import '../../../api/client';",
      ),
    ).toEqual([
      importReport(1, '../../../design-system/components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE),
      importReport(2, '../../../api/client', API_CLIENT_IMPORT_MESSAGE),
    ]);
  });

  it.each(EQUIVALENT_SPECIFIERS)(
    'reports the equivalent path or letter case %s',
    async (source, message) => {
      expect(await lint(FEATURE_TS, `import '${source}';`)).toEqual([
        importReport(1, source, message),
      ]);
    },
  );

  it('reports re-exports of the API client', async () => {
    expect(
      await lint(
        FEATURE_TS,
        "export * from '../../api//client';",
        "export { apiFetch } from '../../api/client';",
      ),
    ).toEqual([
      importReport(1, '../../api//client', API_CLIENT_IMPORT_MESSAGE),
      importReport(2, '../../api/client', API_CLIENT_IMPORT_MESSAGE),
    ]);
  });

  it('allows the design-system index and the query layer', async () => {
    expect(
      await lint(
        FEATURE_TS,
        "import '../../design-system';",
        "import '../../design-system/index';",
        "import '../../design-system/index.ts';",
        "import '../../api/queries';",
        "import '../../api/types';",
        "import '../../lib/format';",
      ),
    ).toEqual([]);
  });

  it.each([
    ['src/design-system/index.ts', "import './components/Button';", "import './tokens';"],
    ['src/api/queries.ts', "import './client';", "import './types';"],
    [
      'src/features/actions/ActionsPage.test.tsx',
      "import '../../api/client';",
      "import '../../design-system/components/Button';",
    ],
  ])('does not apply the feature boundaries to %s', async (filePath, ...imports) => {
    expect(await lint(filePath, ...imports)).toEqual([]);
  });

  it.each([
    ['../../design-system/components/Button', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../design-system/components/Button.tsx', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../design-system/tokens', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../design-system/tokens.css', DESIGN_SYSTEM_IMPORT_MESSAGE],
    ['../../api/client', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/client.ts', API_CLIENT_IMPORT_MESSAGE],
    ['../../api/client.js', API_CLIENT_IMPORT_MESSAGE],
  ])('reports import() of %s', async (source, message) => {
    expect(await lint(FEATURE_TS, `export const load = () => import('${source}');`)).toEqual([
      syntaxReport(1, message),
    ]);
  });

  it('reports import() of internals at any feature depth', async () => {
    expect(
      await lint(
        'src/features/actions/sections/KeywordsSection.tsx',
        "export const button = () => import('../../../design-system/components/Button');",
        "export const client = async () => (await import('../../../api/client')).apiFetch;",
      ),
    ).toEqual([
      syntaxReport(1, DESIGN_SYSTEM_IMPORT_MESSAGE),
      syntaxReport(2, API_CLIENT_IMPORT_MESSAGE),
    ]);
  });

  it.each(EQUIVALENT_SPECIFIERS)(
    'reports import() of the equivalent path or letter case %s',
    async (source, message) => {
      expect(await lint(FEATURE_TS, `export const load = () => import('${source}');`)).toEqual([
        syntaxReport(1, message),
      ]);
    },
  );

  it.each(SUFFIXED_SPECIFIERS)(
    'reports %s, whose path portion names a restricted module',
    async (source, message) => {
      expect(await lint(FEATURE_TS, `import '${source}';`)).toEqual([
        importReport(1, source, message),
      ]);
    },
  );

  it.each(SUFFIXED_SPECIFIERS)(
    'reports import() of %s, whose path portion names a restricted module',
    async (source, message) => {
      expect(await lint(FEATURE_TS, `export const load = () => import('${source}');`)).toEqual([
        syntaxReport(1, message),
      ]);
    },
  );

  it('reports an awaited import() of the API client behind a fragment or query', async () => {
    expect(
      await lint(
        FEATURE_TS,
        "export const a = async () => (await import('../../api/client#fragment')).apiFetch('/x');",
        "export const b = async () => (await import('../../api/client?probe=/')).apiFetch('/x');",
        "export const c = async () => (await import('../../api/client.ts?probe=/')).apiFetch('/x');",
      ),
    ).toEqual([
      syntaxReport(1, API_CLIENT_IMPORT_MESSAGE),
      syntaxReport(2, API_CLIENT_IMPORT_MESSAGE),
      syntaxReport(3, API_CLIENT_IMPORT_MESSAGE),
    ]);
  });

  it.each(UNPREFIXED_SPECIFIERS)(
    'reports the root-relative or bare design-system internal %s',
    async (source, message) => {
      expect(await lint(FEATURE_TS, `import '${source}';`)).toEqual([
        importReport(1, source, message),
      ]);
    },
  );

  it.each(UNPREFIXED_SPECIFIERS)(
    'reports import() of the root-relative or bare design-system internal %s',
    async (source, message) => {
      expect(await lint(FEATURE_TS, `export const load = () => import('${source}');`)).toEqual([
        syntaxReport(1, message),
      ]);
    },
  );

  it.each(UNRESTRICTED_SPECIFIERS)(
    'allows a static import and import() of %s',
    async (source) => {
      expect(
        await lint(
          FEATURE_TS,
          `import '${source}';`,
          `export const load = () => import('${source}');`,
        ),
      ).toEqual([]);
    },
  );

  it('reports import() by a non-literal specifier and import.meta.glob', async () => {
    expect(
      await lint(
        FEATURE_TS,
        'export const byTemplate = (name: string) =>',
        '  import(`../../design-system/components/${name}`);',
        'export const byPlainTemplate = () => import(`../../api/queries`);',
        "const specifier = '../../api/client';",
        'export const byIdentifier = () => import(specifier);',
        "export const byGlob = import.meta.glob('../../design-system/components/*.tsx');",
      ),
    ).toEqual([
      syntaxReport(2, UNCHECKED_IMPORT_MESSAGE),
      syntaxReport(3, UNCHECKED_IMPORT_MESSAGE),
      syntaxReport(5, UNCHECKED_IMPORT_MESSAGE),
      syntaxReport(6, UNCHECKED_IMPORT_MESSAGE),
    ]);
  });

  it('reports import() types of the API client and design-system internals', async () => {
    expect(
      await lint(
        FEATURE_TS,
        "export type Client = typeof import('../../api/client');",
        "export type Tokens = typeof import('../../design-system/tokens');",
      ),
    ).toEqual([
      syntaxReport(1, API_CLIENT_IMPORT_MESSAGE),
      syntaxReport(2, DESIGN_SYSTEM_IMPORT_MESSAGE),
    ]);
  });

  it('allows lazy loading and import() of the design-system index and query layer', async () => {
    expect(
      await lint(
        FEATURE_TSX,
        "import { lazy } from 'react';",
        "export const ActionsPage = lazy(() => import('./ActionsPage'));",
        "export const bare = () => import('../../design-system');",
        "export const trailingSlash = () => import('../../design-system/');",
        "export const index = () => import('../../design-system/index');",
        "export const indexTs = () => import('../../design-system/index.ts');",
        "export const queries = () => import('../../api/queries');",
        "export const types = () => import('../../api/types');",
        "export type Queries = typeof import('../../api/queries');",
      ),
    ).toEqual([]);
  });

  it('allows the index by equivalent paths and modules sharing the client prefix', async () => {
    expect(
      await lint(
        FEATURE_TS,
        "import '../../design-system//index';",
        "import '../../design-system/./index';",
        "import '../../api/clientele';",
        "import '../../api/client-types';",
        "export const index = () => import('../../design-system//index');",
        "export const dotIndex = () => import('../../design-system/./index');",
        "export const clientele = () => import('../../api/clientele');",
        "export const clientTypes = () => import('../../api/client-types');",
      ),
    ).toEqual([]);
  });

  it.each([
    'src/router.tsx',
    'src/design-system/index.ts',
    'src/api/queries.ts',
    'src/features/actions/ActionsPage.test.tsx',
  ])('does not apply the import() boundaries to %s', async (filePath) => {
    expect(
      await lint(
        filePath,
        "export const button = () => import('../../design-system/components/Button');",
        "export const client = () => import('../../api/client');",
        'export const byTemplate = (name: string) => import(`../../features/${name}`);',
        "export const modules = import.meta.glob('../../features/**/*.tsx');",
        "export type Client = typeof import('../../api/client');",
      ),
    ).toEqual([]);
  });
});
