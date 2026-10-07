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
  'The SPA takes no build-time configuration; never read VITE_* variables (AAP 0.3.2).';
const DIRECT_HTTP_MESSAGE =
  'Use the TanStack Query hooks in src/api/queries.ts; only src/api/client.ts calls fetch (AAP 0.11.1).';
const PROCESS_MESSAGE = 'process is not available in the browser bundle.';
const DESIGN_SYSTEM_IMPORT_MESSAGE =
  'Import from the design-system index (src/design-system/index.ts) only (AAP 0.4.2).';
const API_CLIENT_IMPORT_MESSAGE =
  'Features use the hooks and re-exported types in src/api/queries.ts and src/api/types.ts, never apiFetch directly.';

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
  it.each([
    FEATURE_TS,
    API_CLIENT,
    API_QUERIES,
    DESIGN_SYSTEM_COMPONENT,
    'src/features/actions/example.test.ts',
    'src/design-system/components/Button.test.tsx',
  ])('reports the dotted, computed and destructured reads in %s', async (filePath) => {
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
  });

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
});
