/**
 * `LoginPage`: the `/login` screen, the product's single admin sign-in (AAP 0.5.5 routes; AAP
 * 0.1.1 "The UI requires a login"; AAP 0.3.2 single admin login).
 *
 * The page posts `{email, password}` to `POST /api/v1/auth/login` through {@link useLogin}. On
 * success the API sets the signed, HttpOnly session cookie, the hook seeds `queryKeys.me`, and
 * the page replaces `/login` with the `redirectTo` path that `src/router.tsx` read from the
 * `redirect` search parameter (default `/actions`, the default page after login).
 *
 * Messages the page shows, each in text beside the field it concerns (WCAG 2.2 SC 3.3.1):
 *
 * - Checks the page runs before sending, each mirroring what the API accepts
 *   (`LoginRequest` in `backend/src/ads_engine/api/schemas.py`; the admin email's shape in
 *   `backend/src/ads_engine/settings.py`), with lengths counted in code points:
 *   - "Enter your email" / "Enter your password": a field left empty;
 *   - "Email must be at most 320 characters": the email, trimmed and lowercased as the API
 *     does, is longer than the API allows;
 *   - "Enter an email address in the form name@example.com": the email has no nonempty part on
 *     each side of its last `@`, or holds whitespace;
 *   - "Password must be at most 1024 characters": the password, as typed, is longer than the API
 *     allows.
 *
 *   Each message sits under its own field, nothing is sent, and focus moves to the first field
 *   in error, the email before the password. Nothing typed is cleared.
 * - 422 `validation_error` naming `body.email` or `body.password`: the API's own message for
 *   each named field, under that field, with focus on the first one and nothing cleared. A 422
 *   naming neither field is handled like any other API failure.
 * - "Email or password is incorrect": 401 `invalid_credentials`. The API answers one message
 *   for an unknown email and a wrong password, so the page never says which part failed.
 * - "Too many attempts. Try again in N seconds": 429 `rate_limited`, from the 6th attempt within
 *   a minute per IP (AAP 0.5.3), with N from the response's `Retry-After`; without a usable
 *   wait, "Too many attempts. Try again shortly."
 * - Any other API failure shows the API's own message (for example "Database unavailable"), and
 *   a failure without an API error shows "Sign-in failed. Try again."
 *
 * After any other API failure (401, 429, any other status, or no response at all) the password
 * field is cleared and focused, so a screen reader reads the message through the field's
 * `aria-describedby`. The message also heads the form in a
 * critical `Banner`. Neither carries `role="alert"`: AAP 0.5.5 reserves it for the
 * open-discrepancies banner.
 *
 * Rules this file keeps:
 * - **Server state only through `../../api/queries`** (AAP 0.11.1). The CSRF header
 *   `X-Requested-With: ads-engine` is added by the API client for this POST, so no header is
 *   set here.
 * - **Design system only** (AAP 0.4.4): `PageShell`, `TopBar`, `SideNav`, `TextField`,
 *   `Button`, `Banner`, and layout through `Stack` and `Inline`; no raw interactive or heading
 *   elements, no styles and no stylesheet of its own. The `<form>` element is plain HTML because
 *   the design system has no form wrapper and a form carries no visual styling.
 * - **Sign-in navigation**: every screen uses `PageShell`, `TopBar` and `SideNav` (AAP 0.5.5),
 *   and `/login` sits outside the authenticated layout that renders the signed-in screens'
 *   navigation, so the page renders its own "Primary" `SideNav`. It holds one link, "Sign in",
 *   to this screen and nothing else: a signed-out visitor is never shown which signed-in
 *   screens exist.
 * - **The password never leaves the form state**: it is measured and sent as typed (never
 *   trimmed), never rendered, logged or echoed in a message, and cleared after an attempt the
 *   API refused for any reason but a field validation error.
 * - **No router import**: `src/router.tsx` imports this page, so `useNavigate` and `Link` come
 *   straight from `@tanstack/react-router`.
 * - **Fixed strings**: the e2e specs (`web/tests/e2e/auth.setup.ts`, `first-start.spec.ts`)
 *   fill the fields labelled "Email" and "Password" and click "Sign in", and wait for
 *   `/actions`. `first-start.spec.ts` also expects the 401 message "Email or password is
 *   incorrect", in the form's banner and under the Password field.
 *
 * Out of scope (AAP 0.7.2): password reset, "remember me", sign-up, OAuth and any multi-user or
 * role UI.
 *
 * @packageDocumentation
 */
import { Link, useNavigate } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode, SubmitEvent } from 'react';

import { isApiError, useLogin } from '../../api/queries';
import type { LoginRequest } from '../../api/types';
import {
  Banner,
  Button,
  Inline,
  PageShell,
  SideNav,
  Stack,
  TextField,
  TopBar,
} from '../../design-system';
import type { SideNavItem, SideNavLinkProps } from '../../design-system';

// ---------------------------------------------------------------------------------------------
// Texts
// ---------------------------------------------------------------------------------------------

/** The product name in the top bar; the page has no signed-in controls beside it. */
const PRODUCT_NAME = 'Ads Action Engine';

/** The page title, rendered by `PageShell.Title` as the page's single level-1 heading. */
const PAGE_TITLE = 'Sign in';

/** Label of the email field; the e2e specs locate the field by this exact text. */
const EMAIL_LABEL = 'Email';

/** Label of the password field; the e2e specs locate the field by this exact text. */
const PASSWORD_LABEL = 'Password';

/** Text of the submit button; the e2e specs click the button by this exact name. */
const SUBMIT_LABEL = 'Sign in';

/** Field error for an email that is empty after trimming. */
const EMAIL_REQUIRED_MESSAGE = 'Enter your email';

/** Field error for an empty password. */
const PASSWORD_REQUIRED_MESSAGE = 'Enter your password';

/**
 * Longest email the API accepts, in code points, counted after the email is trimmed and
 * lowercased: `LoginRequest.email` (`max_length=320`) in `backend/src/ads_engine/api/schemas.py`.
 */
const EMAIL_MAX_LENGTH = 320;

/**
 * Longest password the API accepts, in code points, counted on the password exactly as typed:
 * `LoginRequest.password` (`max_length=1024`) in `backend/src/ads_engine/api/schemas.py`.
 */
const PASSWORD_MAX_LENGTH = 1024;

/** Field error for an email longer than {@link EMAIL_MAX_LENGTH}. */
const EMAIL_TOO_LONG_MESSAGE = `Email must be at most ${EMAIL_MAX_LENGTH} characters`;

/**
 * Field error for an email without the shape the API requires of the admin email: a part before
 * and after its last `@`, and no whitespace.
 */
const EMAIL_FORMAT_MESSAGE = 'Enter an email address in the form name@example.com';

/** Field error for a password longer than {@link PASSWORD_MAX_LENGTH}. */
const PASSWORD_TOO_LONG_MESSAGE = `Password must be at most ${PASSWORD_MAX_LENGTH} characters`;

/**
 * Message for 401 `invalid_credentials`. One message for an unknown email and a wrong password,
 * as the API sends, so the page never reveals which part failed.
 */
const INVALID_CREDENTIALS_MESSAGE = 'Email or password is incorrect';

/** Message for 429 `rate_limited` when the response gave no usable `Retry-After`. */
const RATE_LIMITED_SHORTLY_MESSAGE = 'Too many attempts. Try again shortly.';

/** Message for a failure that is not an API error, or an API error without a message. */
const GENERIC_FAILURE_MESSAGE = 'Sign-in failed. Try again.';

/** Where a successful sign-in goes when `redirectTo` is not a safe internal path. */
const DEFAULT_REDIRECT = '/actions';

// ---------------------------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------------------------

/**
 * Accessible name of the page's navigation landmark: the name the signed-in screens give their
 * primary navigation, so the landmark is the same one on every screen.
 */
const NAV_LABEL = 'Primary';

/**
 * The navigation's one destination: this screen, marked current. The signed-in screens are
 * deliberately absent, so a signed-out visitor is never told which paths exist, as the
 * router's catch-all never tells them either.
 */
const NAV_ITEMS: readonly SideNavItem[] = Object.freeze([
  { key: 'login', label: PAGE_TITLE, href: '/login', current: true },
]);

/**
 * `activeProps` for the navigation's router link. TanStack `Link` adds an `active` class to an
 * active link unless `activeProps` is given; an empty object keeps the class list exactly the
 * `SideNav` contract (`ds-sidenav__link ds-sidenav__link--current`).
 */
const NO_LINK_STATE_PROPS = Object.freeze({});

/**
 * `SideNav`'s `renderLink`: a TanStack `Link` to `/login` carrying the class contract and
 * `aria-current` that `SideNav` built. It keeps the current search, so activating it never drops
 * the `redirect` a sign-in returns to. The one item's destination is always `/login`.
 *
 * @param _item - The item being rendered; its destination is also `linkProps.href`.
 * @param linkProps - The props `SideNav` built for the link.
 * @returns The link element.
 */
function renderNavLink(_item: SideNavItem, linkProps: SideNavLinkProps): ReactNode {
  return (
    <Link
      to="/login"
      search={true}
      className={linkProps.className}
      aria-current={linkProps['aria-current']}
      activeProps={NO_LINK_STATE_PROPS}
    >
      {linkProps.children}
    </Link>
  );
}

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** The two fields focus can be moved to. */
type FieldName = 'email' | 'password';

/**
 * A request to move focus to a field once the render that shows its error has committed. `seq`
 * changes on every request, so asking for the same field twice still moves focus again.
 */
interface FocusRequest {
  field: FieldName;
  seq: number;
}

/** A validation message per field, each shown under its own field. */
type FieldErrors = Partial<Record<FieldName, string>>;

/** The fields in focus order: the first one in error takes focus. */
const FIELD_ORDER: readonly FieldName[] = Object.freeze(['email', 'password']);

/**
 * The code-point ranges that are whitespace to the API, those for which Python's `str.isspace()`
 * is true (Python 3.14, Unicode 16): `LoginRequest` strips them from the ends of the email, and
 * the admin email may hold none (`APP_ADMIN_EMAIL` in `backend/src/ads_engine/settings.py`).
 * JavaScript's `\s` and `trim()` differ (they add U+FEFF and lack U+001C–U+001F and U+0085), so
 * they are not used: the page measures and checks exactly the value the API will.
 */
const SERVER_WHITESPACE_RANGES: readonly (readonly [number, number])[] = Object.freeze([
  [0x09, 0x0d],
  [0x1c, 0x20],
  [0x85, 0x85],
  [0xa0, 0xa0],
  [0x1680, 0x1680],
  [0x2000, 0x200a],
  [0x2028, 0x2029],
  [0x202f, 0x202f],
  [0x205f, 0x205f],
  [0x3000, 0x3000],
]);

/**
 * The prefix of each field's `loc: msg` pair in a 422 `validation_error` message: the API
 * locates a request-body field as `body.<key>` (`_validation_message` in
 * `backend/src/ads_engine/api/app.py`).
 */
const SERVER_FIELD_PREFIXES: Readonly<Record<FieldName, string>> = Object.freeze({
  email: 'body.email: ',
  password: 'body.password: ',
});

/**
 * The separator between two `loc: msg` pairs of a 422 message: the API joins them with "; ".
 * It splits only where a request location (`body`, `query`, `path`, `header` or `cookie`, with
 * optional dotted parts) follows, so a "; " inside one message never splits it.
 */
const VALIDATION_PAIR_SEPARATOR = /; (?=(?:body|query|path|header|cookie)(?:\.[^\s:;]+)*: )/u;

/** The suffix the API appends to a 422 message when it left further pairs out. */
const VALIDATION_MORE_SUFFIX = / \(and \d+ more\)$/u;

/**
 * Whether `char` is whitespace to the API (see {@link SERVER_WHITESPACE_RANGES}).
 *
 * @param char - One code point, as iterating a string yields it.
 * @returns `true` when Python's `str.isspace()` is true for it.
 */
function isServerWhitespace(char: string): boolean {
  const code = char.codePointAt(0);
  return (
    code !== undefined &&
    SERVER_WHITESPACE_RANGES.some(([low, high]) => code >= low && code <= high)
  );
}

/**
 * `value` without its leading and trailing whitespace, as Python's `str.strip()` returns it: the
 * trim the API applies to the email before it lowercases, measures and compares it.
 *
 * @param value - The email as typed.
 * @returns The trimmed email, which is what the page sends.
 */
function stripServerWhitespace(value: string): string {
  const chars = Array.from(value);
  const first = chars.findIndex((char) => !isServerWhitespace(char));
  if (first === -1) {
    return '';
  }
  const last = chars.findLastIndex((char) => !isServerWhitespace(char));
  return chars.slice(first, last + 1).join('');
}

/**
 * The length of `value` in code points, as Python's `len` counts the API's length limits. A
 * character outside the Basic Multilingual Plane counts once, although it is two UTF-16 units.
 *
 * @param value - The text to measure.
 * @returns The number of code points.
 */
function codePointLength(value: string): number {
  return Array.from(value).length;
}

/**
 * Whether `email` has the shape the API requires of the admin email (`APP_ADMIN_EMAIL` in
 * `backend/src/ads_engine/settings.py`): a nonempty part before and after its last `@`, and no
 * whitespace anywhere. The check is exactly that and no stricter, so every admin email the API
 * starts with can be typed here.
 *
 * @param email - The trimmed, lowercased email.
 * @returns `true` when the shape holds.
 */
function hasAdminEmailShape(email: string): boolean {
  const at = email.lastIndexOf('@');
  return at > 0 && at < email.length - 1 && !Array.from(email).some(isServerWhitespace);
}

/**
 * The email field's error, or `undefined` when the API can accept the email. The checks run on
 * the value the API compares: trimmed as `str.strip()` trims, then lowercased.
 *
 * - empty → {@link EMAIL_REQUIRED_MESSAGE};
 * - more than {@link EMAIL_MAX_LENGTH} code points → {@link EMAIL_TOO_LONG_MESSAGE};
 * - not the admin email's shape ({@link hasAdminEmailShape}) → {@link EMAIL_FORMAT_MESSAGE}.
 *
 * @param email - The email as typed.
 * @returns The message to show under the email field, if any.
 */
function validateEmail(email: string): string | undefined {
  const normalised = stripServerWhitespace(email).toLowerCase();
  if (normalised === '') {
    return EMAIL_REQUIRED_MESSAGE;
  }
  if (codePointLength(normalised) > EMAIL_MAX_LENGTH) {
    return EMAIL_TOO_LONG_MESSAGE;
  }
  return hasAdminEmailShape(normalised) ? undefined : EMAIL_FORMAT_MESSAGE;
}

/**
 * The password field's error, or `undefined` when the API can accept the password. The password
 * is measured exactly as typed, never trimmed, and never appears in the message.
 *
 * - empty → {@link PASSWORD_REQUIRED_MESSAGE};
 * - more than {@link PASSWORD_MAX_LENGTH} code points → {@link PASSWORD_TOO_LONG_MESSAGE}.
 *
 * @param password - The password as typed.
 * @returns The message to show under the password field, if any.
 */
function validatePassword(password: string): string | undefined {
  if (password === '') {
    return PASSWORD_REQUIRED_MESSAGE;
  }
  return codePointLength(password) > PASSWORD_MAX_LENGTH ? PASSWORD_TOO_LONG_MESSAGE : undefined;
}

/**
 * The field errors of a 422 `validation_error` that names the email or the password, so each
 * message is shown under the field it concerns.
 *
 * The API's message is up to three `loc: msg` pairs joined by "; ", with " (and N more)" when it
 * left some out (`_validation_message` in `backend/src/ads_engine/api/app.py`). Each pair
 * located at `body.email` or `body.password` gives that field the pair's `msg`; two pairs for
 * one field are joined by "; ". The API builds `msg` from the location and its own message
 * template only, never from the submitted value, so no password is shown.
 *
 * @param error - The mutation's error.
 * @returns The field errors, or `undefined` for any other failure, including a 422 that names
 *   neither field.
 */
function serverFieldErrors(error: unknown): FieldErrors | undefined {
  if (!isApiError(error) || error.status !== 422 || error.code !== 'validation_error') {
    return undefined;
  }
  const errors: FieldErrors = {};
  const pairs = error.message.replace(VALIDATION_MORE_SUFFIX, '').split(VALIDATION_PAIR_SEPARATOR);
  for (const pair of pairs) {
    for (const field of FIELD_ORDER) {
      const prefix = SERVER_FIELD_PREFIXES[field];
      const detail = pair.startsWith(prefix) ? pair.slice(prefix.length).trim() : '';
      if (detail !== '') {
        const previous = errors[field];
        errors[field] = previous === undefined ? detail : `${previous}; ${detail}`;
      }
    }
  }
  return FIELD_ORDER.some((field) => errors[field] !== undefined) ? errors : undefined;
}

/**
 * The user-facing message for a failed `POST /auth/login`.
 *
 * - 401 `invalid_credentials` → {@link INVALID_CREDENTIALS_MESSAGE};
 * - 429 `rate_limited` → the wait in whole seconds from `Retry-After` when it is positive,
 *   otherwise {@link RATE_LIMITED_SHORTLY_MESSAGE}. A wait of 0 seconds says "shortly" rather
 *   than "in 0 seconds";
 * - any other `ApiError` → its message, which the API client guarantees is safe to show;
 * - anything else → {@link GENERIC_FAILURE_MESSAGE}.
 *
 * The message never contains the submitted password.
 *
 * @param error - The mutation's error.
 * @returns The text shown in the banner and under the password field.
 */
function loginErrorMessage(error: unknown): string {
  if (!isApiError(error)) {
    return GENERIC_FAILURE_MESSAGE;
  }
  if (error.code === 'invalid_credentials') {
    return INVALID_CREDENTIALS_MESSAGE;
  }
  if (error.code === 'rate_limited') {
    const seconds = error.retryAfter;
    if (seconds === undefined || seconds <= 0) {
      return RATE_LIMITED_SHORTLY_MESSAGE;
    }
    return `Too many attempts. Try again in ${seconds} ${seconds === 1 ? 'second' : 'seconds'}`;
  }
  return error.message.trim() === '' ? GENERIC_FAILURE_MESSAGE : error.message;
}

/**
 * Whether `value` contains a C0 control character or DEL. The URL parser strips tabs and line
 * breaks anywhere in a URL, so `/\t/evil.example` would resolve as the protocol-relative
 * `//evil.example`; such a path is never followed.
 */
function hasControlCharacter(value: string): boolean {
  for (const char of value) {
    const code = char.codePointAt(0);
    if (code !== undefined && (code < 0x20 || code === 0x7f)) {
      return true;
    }
  }
  return false;
}

/**
 * The path to open after sign-in: `redirectTo` when it is a same-origin path, otherwise
 * {@link DEFAULT_REDIRECT}.
 *
 * `src/router.tsx` has already dropped unsafe `redirect` values; this check is defence in depth,
 * because the router navigates to an `href` that carries a scheme or an authority with a
 * full-document load. A safe path starts with exactly one `/`: it is not empty, not
 * protocol-relative (`//host`, or `/\host`, which browsers read the same way), carries no scheme
 * and holds no control character.
 *
 * @param redirectTo - The path `src/router.tsx` passed in, possibly with a search such as
 *   `/actions?run=42`.
 * @returns A path that stays inside the SPA.
 */
function safeRedirectTarget(redirectTo: string): string {
  const internal =
    redirectTo.startsWith('/') &&
    !redirectTo.startsWith('//') &&
    !redirectTo.startsWith('/\\') &&
    !hasControlCharacter(redirectTo);
  return internal ? redirectTo : DEFAULT_REDIRECT;
}

// ---------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------

/** Props of {@link LoginPage}. */
export interface LoginPageProps {
  /** Internal path to open after sign-in, already sanitised by `src/router.tsx`. */
  redirectTo: string;
}

/**
 * The `/login` screen: the page frame with the product name, the sign-in navigation, the
 * "Sign in" title, and a form with the Email and Password fields and the "Sign in" button.
 *
 * The `/login` route sits outside the authenticated layout, so the page renders its own
 * `PageShell` with no signed-in controls. Because every screen uses `PageShell`, `TopBar` and
 * `SideNav` (AAP 0.5.5), it also renders its own "Primary" `SideNav`, holding only the current
 * "Sign in" link, which keeps the current search (and so the `redirect`) when activated; no
 * signed-in screen is listed.
 *
 * Behaviour:
 * - Pressing Enter in either field submits the form natively, because the submit button sits in
 *   the same `<form>`. `noValidate` turns off the browser's own bubbles, so the page's messages
 *   are the only ones shown.
 * - While the request is pending the button is busy and disabled, which also blocks a second
 *   submission; the fields stay editable.
 * - A field the API would refuse (empty, too long, or an email without the admin email's shape)
 *   is reported under that field, nothing is sent, and focus moves to the first field in error.
 *   The email is trimmed as the API trims it before it is checked and sent; the password is
 *   checked and sent exactly as typed.
 * - On success the page navigates to `redirectTo`, search included, replacing `/login` in the
 *   history so Back does not return to the form. A `redirectTo` that is not a same-origin path
 *   falls back to `/actions`.
 * - On a 422 `validation_error` naming the email or the password, the API's message for each
 *   named field sits under that field and focus moves to the first one; nothing is cleared.
 * - On any other failure the message heads the form and sits under the password field, the
 *   password is cleared, and focus moves to the password field once the message has rendered.
 *
 * @example
 * ```tsx
 * // src/router.tsx, for the /login route:
 * <LoginPage redirectTo={search.redirect ?? '/actions'} />
 * ```
 *
 * @param props - See {@link LoginPageProps}.
 * @returns The sign-in page.
 */
export function LoginPage({ redirectTo }: LoginPageProps) {
  const login = useLogin();
  const navigate = useNavigate();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [emailError, setEmailError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [formError, setFormError] = useState<string | undefined>(undefined);
  const [focusRequest, setFocusRequest] = useState<FocusRequest | undefined>(undefined);

  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  // Focus moves after the render that shows the message has committed, so the field already
  // carries aria-invalid and the message in aria-describedby when a screen reader announces it.
  // A field that already has focus (Enter pressed in it) is blurred first, so the screen reader
  // announces it again with its new message instead of staying silent.
  useEffect(() => {
    if (focusRequest === undefined) {
      return;
    }
    const target = focusRequest.field === 'email' ? emailRef.current : passwordRef.current;
    if (target === null) {
      return;
    }
    if (target.ownerDocument.activeElement === target) {
      target.blur();
    }
    target.focus();
  }, [focusRequest]);

  /** Asks the effect above to focus `field` after the next commit. */
  const requestFocus = (field: FieldName) => {
    setFocusRequest((previous) => ({ field, seq: (previous?.seq ?? 0) + 1 }));
  };

  const handleEmailChange = (value: string) => {
    setEmail(value);
    setEmailError(undefined);
  };

  const handlePasswordChange = (value: string) => {
    setPassword(value);
    setPasswordError(undefined);
  };

  const handleSubmit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (login.isPending) {
      return;
    }

    const nextEmailError = validateEmail(email);
    const nextPasswordError = validatePassword(password);
    setEmailError(nextEmailError);
    setPasswordError(nextPasswordError);
    setFormError(undefined);
    if (nextEmailError !== undefined) {
      requestFocus('email');
      return;
    }
    if (nextPasswordError !== undefined) {
      requestFocus('password');
      return;
    }

    const credentials = { email: stripServerWhitespace(email), password } satisfies LoginRequest;
    login.mutate(credentials, {
      onSuccess: () => {
        void navigate({ href: safeRedirectTarget(redirectTo), replace: true });
      },
      onError: (error) => {
        // A field the API refused gets its message under that field, as the page's own checks
        // do, and nothing typed is cleared.
        const fieldErrors = serverFieldErrors(error);
        if (fieldErrors !== undefined) {
          setEmailError(fieldErrors.email);
          setPasswordError(fieldErrors.password);
          requestFocus(fieldErrors.email !== undefined ? 'email' : 'password');
          return;
        }
        setFormError(loginErrorMessage(error));
        setPassword('');
        requestFocus('password');
      },
    });
  };

  return (
    <PageShell
      topBar={<TopBar productName={PRODUCT_NAME} />}
      sideNav={<SideNav label={NAV_LABEL} items={NAV_ITEMS} renderLink={renderNavLink} />}
    >
      <PageShell.Title>{PAGE_TITLE}</PageShell.Title>
      <form noValidate onSubmit={handleSubmit}>
        <Stack gap={4}>
          {formError !== undefined ? <Banner tone="critical" title={formError} /> : null}
          <TextField
            label={EMAIL_LABEL}
            type="email"
            name="email"
            autoComplete="username"
            required
            value={email}
            onChange={handleEmailChange}
            error={emailError}
            ref={emailRef}
          />
          <TextField
            label={PASSWORD_LABEL}
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={handlePasswordChange}
            error={passwordError ?? formError}
            ref={passwordRef}
          />
          {/* The action row keeps the button at its own width instead of the column's. */}
          <Inline>
            <Button type="submit" variant="primary" loading={login.isPending}>
              {SUBMIT_LABEL}
            </Button>
          </Inline>
        </Stack>
      </form>
    </PageShell>
  );
}

