/**
 * Vite 8 build and dev-server configuration, and the Vitest 5 configuration (the `test` key), for
 * the Ads Action Engine web client. One file serves `npm run dev`, `npm run build` and `npm test`.
 * There is deliberately no separate `vitest.config.ts`, and Vitest 5 no longer looks for a config
 * in ancestor directories, so this file must stay in `web/`.
 *
 * Environment variable (dev server only):
 * - `WEB_DEV_API_PROXY`: the origin the dev server forwards `/api` to. It defaults to
 *   `http://localhost:8080`, the Compose `web` service: nginx, which forwards `/api/` to
 *   `api:8000`, because the `api` port itself is not published. Point it elsewhere to develop
 *   against an API started another way, for example a uvicorn on the host:
 *   `WEB_DEV_API_PROXY=http://127.0.0.1:8000 npm run dev`. A blank value counts as unset, as
 *   everywhere else in the project, and anything but an absolute `http://` or `https://` URL
 *   fails the config load.
 *
 *   It is read here, in Node, when the config loads, and never reaches the browser bundle: it is
 *   deliberately not `VITE_`-prefixed, so Vite does not expose it through `import.meta.env`, and
 *   this config has no `define`. No container reads it, so it is not in the root `.env.example`.
 *
 *   An `https://` target's certificate chain and its own hostname are always verified, and no
 *   setting disables that. To trust a development CA or a self-signed upstream certificate, name
 *   its PEM file in Node's own `NODE_EXTRA_CA_CERTS` when starting the dev server (see the
 *   example below): Node reads it once at start-up and adds it to its bundled roots. This config
 *   never reads it.
 *
 * The bundle carries no build-time configuration at all. The client calls the relative path
 * `/api/v1`, served by nginx in the stack and by the proxy below in development, and every window
 * length, preset and threshold comes from `GET /api/v1/config` at run time. No secret can
 * therefore reach the browser through this file.
 *
 * Content Security Policy: nginx serves the build with `default-src 'self'`, so the built
 * `index.html` may reference only external, same-origin module scripts and stylesheets. Vite
 * emits exactly that from `web/index.html`. No plugin that injects inline `<script>` or `<style>`
 * belongs here, and assets are never inlined as `data:` URIs (see `build.assetsInlineLimit`).
 *
 * Site icon: the product ships no icon set, so `index.html` links no icon. nginx answers the
 * browser's automatic `/favicon.ico` request with an empty 204, and the dev and preview servers
 * do the same through {@link noFaviconPlugin}, so no server logs a 404 or a console error for it.
 *
 * Imports in `src/` are relative and `tsconfig.json` sets no `paths`, so there is no
 * `resolve.alias`: tsc, Vite and Vitest resolve every module the same way.
 *
 * @example
 * ```sh
 * npm run dev                                          # http://localhost:5173, /api -> :8080
 * WEB_DEV_API_PROXY=http://localhost:9999 npm run dev  # /api -> :9999
 * NODE_EXTRA_CA_CERTS=./dev-ca.pem WEB_DEV_API_PROXY=https://api.dev.example:8443 npm run dev
 * npm run build                                        # dist/index.html and dist/assets/*
 * npm test                                             # Vitest in jsdom, with coverage/
 * ```
 */
import { Agent as HttpsAgent } from 'node:https';
import { isIP } from 'node:net';

import react from '@vitejs/plugin-react';
import type { Connect, Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

// ---------------------------------------------------------------------------------------------
// Dev-server proxy target
// ---------------------------------------------------------------------------------------------

/** The local stack's nginx (`web` service), which forwards `/api/` to the API. */
const DEFAULT_API_PROXY_TARGET = 'http://localhost:8080';

/** URL schemes the proxy can forward to. */
const PROXY_PROTOCOLS: ReadonlySet<string> = new Set(['http:', 'https:']);

/**
 * The origin the dev server forwards `/api` to: `WEB_DEV_API_PROXY` when it holds a value,
 * otherwise {@link DEFAULT_API_PROXY_TARGET}.
 *
 * A blank or whitespace-only value counts as unset. A value that is not an absolute `http://` or
 * `https://` URL fails the config load at once with an error naming the variable, rather than
 * every proxied request failing later with an opaque proxy error. The message names at most the
 * value's scheme, never the value itself, so a URL carrying credentials is never echoed to the
 * terminal. For the same reason the URL parser's own error is deliberately not attached as
 * `cause`: Node's invalid-URL error keeps the raw input, and Vite prints the whole error chain.
 *
 * @param value - The raw environment value, `undefined` when the variable is not set.
 * @returns The proxy target, trimmed.
 * @throws Error when a non-blank value is not an absolute http(s) URL.
 */
function resolveApiProxyTarget(value: string | undefined): string {
  const candidate = value?.trim() ?? '';
  if (candidate === '') {
    return DEFAULT_API_PROXY_TARGET;
  }
  const expected =
    'WEB_DEV_API_PROXY must be an absolute http:// or https:// URL, for example ' +
    DEFAULT_API_PROXY_TARGET;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new Error(`${expected}; the value set is not a URL`);
  }
  if (!PROXY_PROTOCOLS.has(url.protocol)) {
    // A value without a scheme, such as localhost:8080, parses with "localhost:" as its scheme.
    throw new Error(`${expected}; the value set has the scheme "${url.protocol}"`);
  }
  return candidate;
}

/** Where `npm run dev` sends `/api` requests. Read once, in Node, when the config loads. */
const apiProxyTarget = resolveApiProxyTarget(process.env.WEB_DEV_API_PROXY);

/**
 * The TLS agent for an `https://` proxy target, which makes Node verify the upstream certificate
 * against the target's own hostname; `undefined` for an `http://` target, which needs none.
 *
 * Without it, Node derives both the SNI name and the name it checks the certificate against from
 * the forwarded Host header, which the proxy keeps as the dev origin's (`localhost:5173`, see
 * `changeOrigin` below). Every https:// upstream would then be verified as `localhost`: its own
 * valid certificate would be rejected, and any trusted certificate issued for `localhost` would
 * be accepted in its place. An IP-address target sends no SNI, since RFC 6066 allows no address
 * there, and its certificate must list that address.
 *
 * @param target - The proxy target, an absolute http(s) URL from {@link resolveApiProxyTarget}.
 * @returns An agent pinned to the target's hostname, or `undefined` for a plain http target.
 */
function resolveApiProxyAgent(target: string): HttpsAgent | undefined {
  const url = new URL(target);
  if (url.protocol !== 'https:') {
    return undefined;
  }
  // URL keeps an IPv6 literal in brackets, while Node's TLS checks expect the bare address.
  const hostname =
    url.hostname.startsWith('[') && url.hostname.endsWith(']')
      ? url.hostname.slice(1, -1)
      : url.hostname;
  // An empty servername sends no SNI and makes Node check the certificate against the address.
  return new HttpsAgent({ servername: isIP(hostname) === 0 ? hostname : '' });
}

/** The TLS agent for {@link apiProxyTarget}, created once with it when the config loads. */
const apiProxyAgent = resolveApiProxyAgent(apiProxyTarget);

// ---------------------------------------------------------------------------------------------
// Site icon
// ---------------------------------------------------------------------------------------------

/** The path a browser requests a site icon from on its own when the page links none. */
const FAVICON_PATH = '/favicon.ico';

/** The request methods answered at {@link FAVICON_PATH}: a browser fetches an icon with GET. */
const FAVICON_METHODS: ReadonlySet<string> = new Set(['GET', 'HEAD']);

/** HTTP 204 No Content: the answer at {@link FAVICON_PATH}, with an empty body. */
const NO_CONTENT = 204;

/**
 * Answers `GET` and `HEAD` `/favicon.ico` with an empty 204, as nginx.conf's
 * `location = /favicon.ico { return 204; }` does in the stack, and passes every other request on.
 * Only the path is compared, so a query string (`/favicon.ico?v=2`) gets the same answer.
 *
 * @param req - The incoming request; its `url` is the path and query.
 * @param res - The response, ended here for a favicon request.
 * @param next - Hands any other request to the next middleware.
 */
const answerFavicon: Connect.NextHandleFunction = (req, res, next) => {
  const path = req.url?.split('?', 1)[0];
  if (path !== FAVICON_PATH || req.method === undefined || !FAVICON_METHODS.has(req.method)) {
    next();
    return;
  }
  res.statusCode = NO_CONTENT;
  res.end();
};

/**
 * The plugin that gives `npm run dev` and `vite preview` nginx's answer at `/favicon.ico`.
 *
 * The product ships no icon set until the Blitzy OS icon source is available, so `index.html`
 * links no icon, and no icon file or `data:` URI (which the Content Security Policy would block
 * in the stack anyway) stands in for one. Browsers still request `/favicon.ico` by themselves.
 * nginx answers that with 204, while the dev and preview servers would answer 404, which the
 * browser logs as a console error on every fresh load. The middleware is registered directly in
 * each server hook, so it runs before Vite's own static and fallback handling.
 *
 * @returns The plugin, named `ads-engine:no-favicon`.
 */
function noFaviconPlugin(): Plugin {
  return {
    name: 'ads-engine:no-favicon',
    configureServer(server) {
      server.middlewares.use(answerFavicon);
    },
    configurePreviewServer(server) {
      server.middlewares.use(answerFavicon);
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------------------------

export default defineConfig({
  plugins: [react(), noFaviconPlugin()],

  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    // Every asset is emitted as its own file, never inlined as a data: URI: nginx's
    // `default-src 'self'` policy blocks data: images, fonts and media, so an inlined asset
    // would silently fail to load in production while working in development.
    assetsInlineLimit: 0,
  },

  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // The session cookie is host-scoped to `localhost`, so the browser sends it to the dev
      // origin (localhost:5173) and to the stack (localhost:8080) alike, and it is not Secure on
      // the local http:// base URL. The proxy therefore forwards requests with their Host header
      // kept (`changeOrigin: false`).
      //
      // An https:// target's certificate chain is verified against Node's trust store
      // (`secure: true`), and its hostname against the target's own name, not the kept Host
      // header (`agent`, see resolveApiProxyAgent). Running this proxy on a developer's machine
      // does not secure its hop to the upstream, which carries the login, the session cookie and
      // every API response. A development CA or a self-signed upstream certificate is trusted
      // explicitly, by naming its PEM file in Node's NODE_EXTRA_CA_CERTS when starting the dev
      // server. A request whose upstream fails verification gets 502, and the dev-server log
      // shows the TLS error.
      '/api': {
        target: apiProxyTarget,
        changeOrigin: false,
        secure: true,
        agent: apiProxyAgent,
      },
    },
  },

  test: {
    // Components render into jsdom. Node-only suites (the lint-config, token and format tests)
    // opt out per file with `// @vitest-environment node`.
    environment: 'jsdom',
    // describe, it and expect without imports (tsconfig.json lists vitest/globals), and Testing
    // Library's automatic cleanup after each test, which registers through the global afterEach,
    // so no setup file is needed.
    globals: true,
    // Unit and component tests only. Playwright specs live under tests/e2e/ and never run here.
    // Vitest 5 clears mocks before each test and fails a test that leaves an async assertion
    // un-awaited, both by default, so neither needs configuring.
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      // Needs @vitest/coverage-v8 at the same version as vitest.
      provider: 'v8',
      // Stated explicitly: Vitest 5 moved several default output directories under .vitest/,
      // while the project (.gitignore included) expects the report in web/coverage/.
      reportsDirectory: 'coverage',
      reporter: ['text', 'html', 'json-summary'],
      // Every source file counts, including those no test imports, so an untested module lowers
      // the figure instead of disappearing from it.
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        // Only the bootstrap: it builds the query client and router, renders them, and empties
        // and reloads a page the browser keeps in its back/forward cache.
        'src/main.tsx',
        'src/**/*.d.ts',
        // Test-only support code (the typed API payload fixtures) is never shipped, so it must
        // not count toward the application's coverage.
        'src/test/**',
      ],
      thresholds: {
        // Web overall: at least 70% of statements.
        statements: 70,
        // The five action sections and their card: at least 80% of statements, as one aggregate
        // over the folder. perFile is left unset on purpose: in Vitest 5 a glob threshold no
        // longer inherits a top-level perFile, so this stays an aggregate.
        'src/features/actions/**': {
          statements: 80,
        },
      },
    },
  },
});
