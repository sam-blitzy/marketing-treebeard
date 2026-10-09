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
 * - "Enter your email" / "Enter your password": a field left empty. Nothing is sent, and focus
 *   moves to the first empty field.
 * - "Email or password is incorrect": 401 `invalid_credentials`. The API answers one message
 *   for an unknown email and a wrong password, so the page never says which part failed.
 * - "Too many attempts. Try again in N seconds": 429 `rate_limited`, from the 6th attempt within
 *   a minute per IP (AAP 0.5.3), with N from the response's `Retry-After`; without a usable
 *   wait, "Too many attempts. Try again shortly."
 * - Any other API failure shows the API's own message (for example "Database unavailable"), and
 *   a failure without an API error shows "Sign-in failed. Try again."
 *
 * After an API failure the password field is cleared and focused, so a screen reader reads the
 * message through the field's `aria-describedby`. The message also heads the form in a
 * critical `Banner`. Neither carries `role="alert"`: AAP 0.5.5 reserves it for the
 * open-discrepancies banner.
 *
 * Rules this file keeps:
 * - **Server state only through `../../api/queries`** (AAP 0.11.1). The CSRF header
 *   `X-Requested-With: ads-engine` is added by the API client for this POST, so no header is
 *   set here.
 * - **Design system only** (AAP 0.4.4): `PageShell`, `TopBar`, `TextField`, `Button`,
 *   `Banner`, and layout through `Stack` and `Inline`; no raw interactive or heading elements,
 *   no styles and no stylesheet of its own. The `<form>` element is plain HTML because the
 *   design system has no form wrapper and a form carries no visual styling.
 * - **The password never leaves the form state**: it is sent as typed (never trimmed), never
 *   rendered, logged or echoed in a message, and cleared after a failed attempt.
 * - **No router import**: `src/router.tsx` imports this page, so `useNavigate` comes straight
 *   from `@tanstack/react-router`.
 * - **Fixed strings**: the e2e specs (`web/tests/e2e/auth.setup.ts`, `first-start.spec.ts`)
 *   fill the fields labelled "Email" and "Password" and click "Sign in", and wait for
 *   `/actions`.
 *
 * Out of scope (AAP 0.7.2): password reset, "remember me", sign-up, OAuth and any multi-user or
 * role UI.
 *
 * @packageDocumentation
 */
import { useNavigate } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import type { SubmitEvent } from 'react';

import { isApiError, useLogin } from '../../api/queries';
import type { LoginRequest } from '../../api/types';
import { Banner, Button, Inline, PageShell, Stack, TextField, TopBar } from '../../design-system';

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
 * The `/login` screen: the page frame with the product name, the "Sign in" title, and a form
 * with the Email and Password fields and the "Sign in" button.
 *
 * The `/login` route sits outside the authenticated layout, so the page renders its own
 * `PageShell` without side navigation or signed-in controls.
 *
 * Behaviour:
 * - Pressing Enter in either field submits the form natively, because the submit button sits in
 *   the same `<form>`. `noValidate` turns off the browser's own bubbles, so the page's messages
 *   are the only ones shown.
 * - While the request is pending the button is busy and disabled, which also blocks a second
 *   submission; the fields stay editable.
 * - Empty fields are reported under each field and nothing is sent. The email is trimmed before
 *   it is checked and sent; the password is sent exactly as typed.
 * - On success the page navigates to `redirectTo`, search included, replacing `/login` in the
 *   history so Back does not return to the form. A `redirectTo` that is not a same-origin path
 *   falls back to `/actions`.
 * - On failure the message heads the form and sits under the password field, the password is
 *   cleared, and focus moves to the password field once the message has rendered.
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

    const trimmedEmail = email.trim();
    const nextEmailError = trimmedEmail === '' ? EMAIL_REQUIRED_MESSAGE : undefined;
    const nextPasswordError = password === '' ? PASSWORD_REQUIRED_MESSAGE : undefined;
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

    const credentials = { email: trimmedEmail, password } satisfies LoginRequest;
    login.mutate(credentials, {
      onSuccess: () => {
        void navigate({ href: safeRedirectTarget(redirectTo), replace: true });
      },
      onError: (error) => {
        setFormError(loginErrorMessage(error));
        setPassword('');
        requestFocus('password');
      },
    });
  };

  return (
    <PageShell topBar={<TopBar productName={PRODUCT_NAME} />}>
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

