// @vitest-environment node
/**
 * Guards for web/nginx.conf, the web container's server configuration.
 *
 * The file is parsed with nginx's own token rules (quotes, backslash escapes, `#` comments,
 * `{ }` blocks and `;`-terminated directives) into a directive tree, and the tree is held to the
 * properties that keep the edge from leaking what the API never logs, from answering an `/api/`
 * error the SPA cannot read, from answering a well-known file with the SPA document, and from
 * compressing at nginx's weakest gzip level:
 * - nginx's error log has no format, and every line it writes about a request names the raw
 *   request line, query string included, and the referer. The listening socket and every
 *   request on it, before nginx picks a location and after, log to the server's error_log, so
 *   the server declares exactly one, `error_log /dev/null`, and no `location`, named ones and
 *   their `if` blocks included, declares one of its own: each inherits the server's.
 * - The access log is written only in the `redacted` format, and no log format reads a variable
 *   that carries the raw request line, query string, Basic-auth user, referer or user agent.
 * - The SPA document is never stored, so Back after sign-out cannot restore a signed-in page.
 * - `/api/` gives up on an unreachable API well before nginx's 60 s default, and every error
 *   nginx makes there is answered by a named location in the API's error envelope,
 *   `{"error":{"code","message"}}`, as uncacheable application/json.
 * - gzip is on at level 5 or 6, never nginx's default 1, set once for the whole server.
 * - `/robots.txt` answers 200 text/plain with valid robots.txt lines that leave every screen,
 *   `/` included, crawlable and keep crawlers out of `/api/`; `/llms.txt` and `/sitemap.xml`
 *   answer 404. None of the three declares a header or an access log of its own, so each keeps
 *   the server-level security headers and is logged.
 *
 * Whether nginx accepts the file is `nginx -t`'s job on nginx:1.30.5; these tests hold the file
 * to what it must say.
 */
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

/** web/nginx.conf, resolved from this file's URL (web/src/ -> web/). */
const NGINX_CONF_URL = new URL('../nginx.conf', import.meta.url);

/** One parsed directive: its name, its arguments as nginx reads them, and its line. */
interface Directive {
  readonly name: string;
  readonly args: readonly string[];
  readonly line: number;
  /** The directives inside its `{ }`, or `undefined` for a directive that ends with `;`. */
  readonly block: readonly Directive[] | undefined;
}

/** A lexical token: a word (an argument or a directive name) or one of `{`, `}` and `;`. */
interface Token {
  readonly kind: 'word' | '{' | '}' | ';';
  readonly text: string;
  readonly line: number;
}

/** Characters that end an unquoted word, as in nginx's ngx_conf_read_token. */
const WORD_END = new Set([' ', '\t', '\r', '\n', ';', '{']);

/**
 * Resolves backslash escapes the way nginx does once it has read a word: `\"`, `\'` and `\\`
 * become the character itself, `\t`, `\r` and `\n` the control character, and any other
 * backslash stays (regular expressions keep `\?` and `\.`).
 */
function unescape(raw: string): string {
  let out = '';
  for (let i = 0; i < raw.length; i += 1) {
    const ch = raw.charAt(i);
    const next = raw.charAt(i + 1);
    if (ch === '\\' && (next === '"' || next === "'" || next === '\\')) {
      out += next;
      i += 1;
    } else if (ch === '\\' && (next === 't' || next === 'r' || next === 'n')) {
      out += next === 't' ? '\t' : next === 'r' ? '\r' : '\n';
      i += 1;
    } else {
      out += ch;
    }
  }
  return out;
}

/**
 * Splits nginx configuration text into tokens. A backslash escapes the next character inside or
 * outside quotes, `${` does not end a word, and a closing quote must be followed by whitespace,
 * `;`, `{` or `)` (an `if` condition), as nginx requires. Throws on an unterminated quote.
 */
function tokenize(source: string): Token[] {
  const tokens: Token[] = [];
  let line = 1;
  let i = 0;
  while (i < source.length) {
    const ch = source.charAt(i);
    if (ch === '\n') {
      line += 1;
      i += 1;
    } else if (ch === ' ' || ch === '\t' || ch === '\r') {
      i += 1;
    } else if (ch === '#') {
      while (i < source.length && source.charAt(i) !== '\n') {
        i += 1;
      }
    } else if (ch === '{' || ch === '}' || ch === ';') {
      tokens.push({ kind: ch, text: ch, line });
      i += 1;
    } else if (ch === '"' || ch === "'") {
      const startLine = line;
      let raw = '';
      i += 1;
      while (source.charAt(i) !== ch) {
        if (i >= source.length) {
          throw new Error(`nginx.conf:${String(startLine)}: unterminated ${ch} string`);
        }
        const c = source.charAt(i);
        const take = c === '\\' ? 2 : 1;
        const chunk = source.slice(i, i + take);
        line += chunk.split('\n').length - 1;
        raw += chunk;
        i += take;
      }
      i += 1;
      const after = source.charAt(i);
      if (i < source.length && !WORD_END.has(after) && after !== ')') {
        throw new Error(`nginx.conf:${String(line)}: unexpected "${after}" after a quoted word`);
      }
      tokens.push({ kind: 'word', text: unescape(raw), line: startLine });
    } else {
      let raw = '';
      while (i < source.length) {
        const c = source.charAt(i);
        if (c === '\\') {
          raw += source.slice(i, i + 2);
          i += 2;
        } else if (c === '$' && source.charAt(i + 1) === '{') {
          raw += '${';
          i += 2;
        } else if (WORD_END.has(c)) {
          break;
        } else {
          raw += c;
          i += 1;
        }
      }
      tokens.push({ kind: 'word', text: unescape(raw), line });
    }
  }
  return tokens;
}

/** A directive under construction; `block` is filled while its `{ }` is open. */
interface OpenDirective {
  name: string;
  args: string[];
  line: number;
  block: OpenDirective[] | undefined;
}

/**
 * Parses nginx configuration text into its directive tree. Throws on a `;` or `{` with no
 * directive name, a `}` that closes nothing or interrupts a directive, and text that ends inside
 * a directive or a block, so a broken file can never pass as an empty or partial one.
 */
function parse(source: string): readonly Directive[] {
  const root: OpenDirective[] = [];
  const stack: OpenDirective[][] = [root];
  let words: Token[] = [];
  const open = (): OpenDirective[] => {
    const current = stack.at(-1);
    if (current === undefined) {
      throw new Error('nginx.conf: block stack is empty');
    }
    return current;
  };
  for (const token of tokenize(source)) {
    if (token.kind === 'word') {
      words.push(token);
      continue;
    }
    if (token.kind === '}') {
      if (words.length > 0 || stack.length === 1) {
        throw new Error(`nginx.conf:${String(token.line)}: unexpected "}"`);
      }
      stack.pop();
      continue;
    }
    const [nameToken, ...argTokens] = words;
    if (nameToken === undefined) {
      throw new Error(`nginx.conf:${String(token.line)}: unexpected "${token.text}"`);
    }
    const directive: OpenDirective = {
      name: nameToken.text,
      args: argTokens.map(({ text }) => text),
      line: nameToken.line,
      block: token.kind === '{' ? [] : undefined,
    };
    open().push(directive);
    if (directive.block !== undefined) {
      stack.push(directive.block);
    }
    words = [];
  }
  if (words.length > 0 || stack.length !== 1) {
    throw new Error('nginx.conf: unexpected end of file inside a directive or block');
  }
  return root;
}

/** The directives named `name` directly inside `block` (never inside a nested block). */
function directivesNamed(block: readonly Directive[] | undefined, name: string): Directive[] {
  return (block ?? []).filter((directive) => directive.name === name);
}

/** Every directive in `block` and in every block nested in it, depth first. */
function descendants(block: readonly Directive[] | undefined): Directive[] {
  return (block ?? []).flatMap((directive) => [directive, ...descendants(directive.block)]);
}

/** The `location` whose arguments, joined by a space, are `key` (`= /index.html`, `/api/`). */
function locationBlock(locations: readonly Directive[], key: string): Directive {
  const found = locations.filter(({ args }) => args.join(' ') === key);
  if (found.length !== 1 || found[0] === undefined) {
    throw new Error(`expected exactly one "location ${key}", found ${String(found.length)}`);
  }
  return found[0];
}

/** Seconds per nginx time unit (the unit-less default is seconds). */
const TIME_UNITS: Readonly<Record<string, number>> = {
  ms: 0.001,
  s: 1,
  m: 60,
  h: 3600,
  d: 86_400,
  w: 604_800,
  M: 2_592_000,
  y: 31_536_000,
};

/** An nginx time value such as `5s`, `500ms` or `1m30s`, in seconds. Throws on any other text. */
function seconds(value: string): number {
  const parts = [...value.matchAll(/(\d+)(ms|[smhdwMy]?)/g)];
  if (parts.length === 0 || parts.map(([part]) => part).join('') !== value) {
    throw new Error(`not an nginx time value: ${value}`);
  }
  return parts.reduce(
    (total, [, amount = '0', unit = '']) => total + Number(amount) * (TIME_UNITS[unit || 's'] ?? 1),
    0,
  );
}

/**
 * The status codes an `error_page` directive lists: every argument before the target except a
 * `=code` response override.
 */
function errorPageCodes(directive: Directive): string[] {
  return directive.args.slice(0, -1).filter((arg) => !arg.startsWith('='));
}

/** Variables whose value carries the raw request line, query, user, referer or user agent. */
const RAW_LOG_VARIABLES = new Set([
  'request',
  'remote_user',
  'http_referer',
  'args',
  'query_string',
  'request_uri',
  'http_user_agent',
  'http_authorization',
  'http_cookie',
  'request_body',
]);

/** Variable prefixes whose value is one query argument or one cookie, verbatim. */
const RAW_LOG_VARIABLE_PREFIXES = ['arg_', 'cookie_'];

/** The names of the variables `text` references, `$name` and `${name}` alike. */
function variablesIn(text: string): string[] {
  return [...text.matchAll(/\$\{?([A-Za-z0-9_]+)\}?/g)].map(([, name = '']) => name);
}

/** The locations web/nginx.conf is known to declare; any other location is checked as well. */
const KNOWN_LOCATIONS = [
  '/api/',
  '@api_payload_too_large',
  '@api_internal_error',
  '@api_upstream_unavailable',
  '@api_upstream_timeout',
  '@bad_request',
  '@request_header_too_large',
  '@method_not_allowed',
  '@not_implemented',
  '= /.nginx/bad-request',
  '/assets/',
  '= /index.html',
  '= /favicon.ico',
  '= /robots.txt',
  '= /llms.txt',
  '= /sitemap.xml',
  '/',
];

/** The errors nginx itself can make under /api/, each of which needs a JSON answer. */
const API_ERROR_STATUSES = ['400', '405', '413', '500', '502', '503', '504'];

/** The well-known files nginx answers itself instead of falling back to the SPA document. */
const WELL_KNOWN_LOCATIONS = ['= /robots.txt', '= /llms.txt', '= /sitemap.xml'];

/** The SPA's screens, which robots.txt must leave crawlable. */
const SPA_SCREENS = ['/', '/login', '/actions', '/decisions', '/sources', '/settings'];

/**
 * One line of a robots.txt file as RFC 9309 reads it, keys in any letter case: a `user-agent`
 * line naming a product token or `*`, an `allow` or `disallow` line with an empty path or one
 * starting with `/` or `*`, each with an optional trailing `#` comment; a `sitemap` line naming
 * an absolute URL; a comment; or a blank line.
 */
const ROBOTS_LINE = new RegExp(
  [
    String.raw`^\s*(?:`,
    String.raw`(?:user-agent\s*:\s*(?:\*|[A-Za-z_-]+)`,
    String.raw`|(?:allow|disallow)\s*:\s*(?:[/*][^\s#]*)?)\s*(?:#.*)?`,
    String.raw`|sitemap\s*:\s*https?://\S+\s*`,
    String.raw`|#.*`,
    String.raw`)?$`,
  ].join(''),
  'i',
);

/** One allow or disallow rule of a robots.txt group, with its path pattern. */
interface RobotsRule {
  readonly allow: boolean;
  readonly pattern: string;
}

/**
 * The allow and disallow rules robots.txt `body` sets for every crawler: the rules of each group
 * whose user-agent lines include `*`. A group is a run of user-agent lines and the rules after it,
 * up to the next user-agent line.
 */
function rulesForEveryCrawler(body: string): RobotsRule[] {
  const rules: RobotsRule[] = [];
  let agents: string[] = [];
  let inRules = false;
  for (const raw of body.split('\n')) {
    const match = /^([A-Za-z-]+)\s*:\s*([^#]*)/.exec(raw.trim());
    const key = match?.[1]?.toLowerCase();
    const value = match?.[2]?.trim() ?? '';
    if (key === 'user-agent') {
      if (inRules) {
        agents = [];
        inRules = false;
      }
      agents.push(value);
    } else if (key === 'allow' || key === 'disallow') {
      inRules = true;
      if (agents.includes('*')) {
        rules.push({ allow: key === 'allow', pattern: value });
      }
    }
  }
  return rules;
}

/**
 * A robots.txt path pattern as a regular expression anchored at the path's start: `*` matches
 * any run of characters, a final `$` anchors the end, and every other character is literal.
 */
function robotsPattern(pattern: string): RegExp {
  const anchored = pattern.endsWith('$');
  const literal = anchored ? pattern.slice(0, -1) : pattern;
  const source = literal
    .split('*')
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&'))
    .join('.*');
  return new RegExp(`^${source}${anchored ? '$' : ''}`);
}

/**
 * Whether `rules` let a crawler fetch `path`, by RFC 9309's precedence: the matching rule with
 * the longest pattern decides, an allow wins a tie, and a path no rule matches is allowed. An
 * empty pattern matches nothing.
 */
function robotsAllows(rules: readonly RobotsRule[], path: string): boolean {
  let decisive: RobotsRule | undefined;
  for (const rule of rules) {
    if (rule.pattern === '' || !robotsPattern(rule.pattern).test(path)) {
      continue;
    }
    const longer = decisive === undefined || rule.pattern.length > decisive.pattern.length;
    const tieWon = decisive?.pattern.length === rule.pattern.length && rule.allow;
    if (longer || tieWon) {
      decisive = rule;
    }
  }
  return decisive?.allow ?? true;
}

const config = parse(readFileSync(NGINX_CONF_URL, 'utf8'));
const servers = directivesNamed(config, 'server');
const server = servers[0];
const locations = descendants(server?.block).filter(({ name }) => name === 'location');
const namedLocations = locations.filter(({ args }) => args[0]?.startsWith('@') === true);

describe('the parser', () => {
  it('reads quotes, escapes, comments and nested blocks as nginx does', () => {
    const tree = parse(
      [
        '# a comment { ; }',
        'map $a $b { "~^x\\.y$" "1"; default \'it\\\'s\'; }',
        'server { location /x/ { if ($u ~ "^/x/") { return 400 \'{"e":1}\'; } } }',
      ].join('\n'),
    );
    expect(tree.map(({ name, args, line }) => ({ name, args, line }))).toEqual([
      { name: 'map', args: ['$a', '$b'], line: 2 },
      { name: 'server', args: [], line: 3 },
    ]);
    expect(descendants(tree).map(({ name, args }) => [name, ...args])).toEqual([
      ['map', '$a', '$b'],
      ['~^x\\.y$', '1'],
      ['default', "it's"],
      ['server'],
      ['location', '/x/'],
      ['if', '($u', '~', '^/x/', ')'],
      ['return', '400', '{"e":1}'],
    ]);
  });

  it.each([
    ['an unclosed block', 'server { listen 8080;'],
    ['a stray closing brace', 'listen 8080; }'],
    ['a directive cut short by a brace', 'server { listen 8080 }'],
    ['a directive with no terminator', 'listen 8080'],
    ['an unterminated quote', 'return 200 "ok;'],
    ['text glued to a closing quote', 'return 200 "ok"x;'],
  ])('rejects %s', (_label, source) => {
    expect(() => parse(source)).toThrow(/nginx\.conf/);
  });

  it('finds one server block with every known location in web/nginx.conf', () => {
    expect(servers).toHaveLength(1);
    expect(locations.map(({ args }) => args.join(' '))).toEqual(
      expect.arrayContaining(KNOWN_LOCATIONS),
    );
  });
});

describe('web/nginx.conf error log', () => {
  it('sends the error log of the listening socket and every request to /dev/null', () => {
    expect(directivesNamed(server?.block, 'error_log').map(({ args }) => args)).toEqual([
      ['/dev/null'],
    ]);
  });

  it.each(locations.map((location) => [location.args.join(' '), location] as const))(
    'location %s declares no error log of its own, its if blocks included',
    (_key, location) => {
      const own = descendants(location.block)
        .filter(({ name }) => name === 'error_log')
        .map(({ line, args }) => `line ${String(line)}: error_log ${args.join(' ')}`);
      expect(own).toEqual([]);
    },
  );
});

describe('web/nginx.conf access log', () => {
  it('logs the server in the redacted format', () => {
    expect(directivesNamed(server?.block, 'access_log').map(({ args }) => args)).toEqual([
      ['/var/log/nginx/access.log', 'redacted'],
    ]);
  });

  it('writes every access log in the redacted format or not at all', () => {
    const accessLogs = descendants(config).filter(({ name }) => name === 'access_log');
    expect(accessLogs.length).toBeGreaterThan(0);
    for (const { args, line } of accessLogs) {
      const ok = args.join(' ') === 'off' || args[1] === 'redacted';
      expect(ok, `line ${String(line)}: access_log ${args.join(' ')}`).toBe(true);
    }
  });

  it('defines the redacted format without any variable that carries raw request data', () => {
    const formats = descendants(config).filter(({ name }) => name === 'log_format');
    expect(formats.map(({ args }) => args[0])).toContain('redacted');
    for (const { args, line } of formats) {
      const raw = variablesIn(args.slice(1).join('')).filter(
        (name) =>
          RAW_LOG_VARIABLES.has(name) ||
          RAW_LOG_VARIABLE_PREFIXES.some((prefix) => name.startsWith(prefix)),
      );
      expect(raw, `line ${String(line)}: log_format ${args[0] ?? ''}`).toEqual([]);
    }
  });
});

describe('web/nginx.conf caching', () => {
  it('never stores the SPA document: one Cache-Control no-store and no expires', () => {
    const document = locationBlock(locations, '= /index.html');
    const cacheControl = directivesNamed(document.block, 'add_header').filter(
      ({ args }) => args[0]?.toLowerCase() === 'cache-control',
    );
    expect(cacheControl.map(({ args }) => args)).toEqual([['Cache-Control', 'no-store', 'always']]);
    expect(descendants(document.block).filter(({ name }) => name === 'expires')).toEqual([]);
  });
});

describe('web/nginx.conf compression', () => {
  it('turns gzip on at level 5 or 6, never the default 1, with no other level anywhere', () => {
    expect(directivesNamed(server?.block, 'gzip').map(({ args }) => args)).toEqual([['on']]);
    const levels = descendants(config).filter(({ name }) => name === 'gzip_comp_level');
    expect(levels).toHaveLength(1);
    expect(directivesNamed(server?.block, 'gzip_comp_level')).toEqual(levels);
    expect(['5', '6']).toContain(levels[0]?.args.join(' '));
  });
});

describe('web/nginx.conf well-known files', () => {
  const robots = locationBlock(locations, '= /robots.txt');
  const robotsReturns = directivesNamed(robots.block, 'return');
  const robotsBody = robotsReturns[0]?.args[1] ?? '';

  it('answers /robots.txt itself with a 200 as text/plain', () => {
    expect(robotsReturns.map(({ args }) => args[0])).toEqual(['200']);
    expect(directivesNamed(robots.block, 'default_type').map(({ args }) => args)).toEqual([
      ['text/plain'],
    ]);
  });

  it('writes /robots.txt as valid robots.txt lines with a group for every crawler', () => {
    const lines = robotsBody.split('\n');
    for (const [index, line] of lines.entries()) {
      expect(ROBOTS_LINE.test(line), `robots.txt line ${String(index + 1)}: ${line}`).toBe(true);
    }
    expect(lines).toContain('User-agent: *');
  });

  it('keeps every screen crawlable, `/` itself included, and keeps crawlers out of /api/', () => {
    const rules = rulesForEveryCrawler(robotsBody);
    for (const path of SPA_SCREENS) {
      expect(robotsAllows(rules, path), path).toBe(true);
    }
    expect(robotsAllows(rules, '/api/v1/healthz')).toBe(false);
  });

  it.each(['= /llms.txt', '= /sitemap.xml'])('answers %s with a plain 404', (key) => {
    const { block } = locationBlock(locations, key);
    expect(directivesNamed(block, 'return').map(({ args }) => args)).toEqual([['404']]);
  });

  it.each(WELL_KNOWN_LOCATIONS)(
    '%s declares no header or access log of its own, so it keeps the server-level ones',
    (key) => {
      const own = descendants(locationBlock(locations, key).block)
        .filter(({ name }) => name.startsWith('add_header') || name === 'access_log')
        .map(({ line, name, args }) => `line ${String(line)}: ${name} ${args.join(' ')}`);
      expect(own).toEqual([]);
    },
  );
});

describe('web/nginx.conf /api/ errors', () => {
  const api = locationBlock(locations, '/api/');

  it('gives up connecting to the API well before the 60 s default', () => {
    const timeouts = directivesNamed(api.block, 'proxy_connect_timeout');
    expect(timeouts).toHaveLength(1);
    expect(seconds(timeouts[0]?.args[0] ?? '')).toBeLessThan(60);
  });

  it('routes every error nginx makes there to a named location that exists', () => {
    const targets = new Map<string, string>();
    for (const directive of directivesNamed(api.block, 'error_page')) {
      for (const code of errorPageCodes(directive)) {
        targets.set(code, directive.args.at(-1) ?? '');
      }
    }
    expect([...targets.keys()]).toEqual(expect.arrayContaining(API_ERROR_STATUSES));
    const named = new Set(namedLocations.map(({ args }) => args[0]));
    for (const status of API_ERROR_STATUSES) {
      expect(named.has(targets.get(status)), `error_page ${status}`).toBe(true);
    }
  });

  it.each(namedLocations.map((location) => [location.args[0] ?? '', location] as const))(
    '%s answers the API error envelope as uncacheable application/json',
    (_name, location) => {
      expect(directivesNamed(location.block, 'default_type').map(({ args }) => args)).toEqual([
        ['application/json'],
      ]);
      const blocks = [location, ...descendants(location.block).filter(({ name }) => name === 'if')];
      const jsonReturns = blocks.flatMap((block) =>
        directivesNamed(block.block, 'return')
          .filter(({ args }) => args[1]?.startsWith('{') === true)
          .map((directive) => ({ block, directive })),
      );
      expect(jsonReturns.length).toBeGreaterThan(0);
      for (const { block, directive } of jsonReturns) {
        const where = `line ${String(directive.line)}`;
        const headers = directivesNamed(block.block, 'add_header').map(({ args }) => args);
        expect(headers, where).toContainEqual(['Cache-Control', 'no-store', 'always']);
        const body: unknown = JSON.parse(directive.args[1] ?? '');
        expect(body, where).toEqual({
          error: { code: expect.any(String), message: expect.any(String) },
        });
      }
    },
  );
});
