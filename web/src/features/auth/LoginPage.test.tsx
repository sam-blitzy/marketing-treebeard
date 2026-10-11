/**
 * Component tests for `LoginPage`, the `/login` screen and the product's single admin sign-in
 * (AAP 0.1.1 "The UI requires a login"; AAP 0.3.2 single admin login, 5 attempts per minute per
 * IP; AAP 0.5.3 `POST /auth/login`; AAP 0.5.5 `/actions` is the default after login).
 *
 * The page renders inside the test router of `../../test/render`, so the navigation after a
 * successful sign-in is read from `router.state.location` and the history length. The network
 * is stubbed with `../../test/mockApi`, so the real API client builds the request (JSON body,
 * CSRF header) and maps each error response, `Retry-After` included, to the `ApiError` the page
 * turns into its message. The cases cover:
 *
 * - the request: `{email, password}` with the email trimmed and the password sent as typed, and
 *   `X-Requested-With: ads-engine`;
 * - success: the signed-in user seeded into `queryKeys.me` and `/login` replaced by the
 *   `redirect` path, honoured only when it is a same-origin path, otherwise `/actions`;
 * - Enter submitting natively, because the fields and the submit button share one form;
 * - the "Primary" side navigation, holding only the current "Sign in" link (AAP 0.5.5);
 * - validation mirroring the API: empty fields, the admin email's shape, and the 320 and 1024
 *   code-point limits measured as the API measures them, each message under its own field, with
 *   nothing sent and focus on the first field in error;
 * - a 422 `validation_error` naming `body.email` or `body.password`, shown under that field;
 * - 401 `invalid_credentials`, 429 `rate_limited` with and without a usable `Retry-After`, any
 *   other refusal, and the pending state that blocks a second submission;
 * - outages (a 5xx, with or without the API's envelope, and a request that never reached the
 *   server): one message at the head of the form, in the polite status region mounted with the
 *   page, no field marked invalid, nothing typed cleared, and focus back on "Sign in" when it had
 *   fallen to `<body>`.
 *
 * @packageDocumentation
 */
import { apiError, html, json, mockApi } from '../../test/mockApi';
import type { MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../../api/queries';
import type { Me } from '../../api/types';
import { CUSTOM_RUN_ID, makeMe } from '../../test/fixtures';
import { LoginPage } from './LoginPage';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** The admin email of the fixture world, as `POST /auth/login` echoes it. */
const ADMIN_EMAIL = makeMe().email;

/** A password with surrounding and inner spaces, which must reach the API exactly as typed. */
const TYPED_PASSWORD = ' correct horse battery ';

/** The field error for an email without the admin email's `local@domain` shape. */
const EMAIL_FORMAT_MESSAGE = 'Enter an email address in the form name@example.com';

/** A 12-code-point domain part, for emails built to an exact length. */
const EMAIL_DOMAIN = '@example.com';

/** The page's three controls, found as a user finds them: by their visible names. */
function controls(): {
  email: HTMLInputElement;
  password: HTMLInputElement;
  submit: HTMLButtonElement;
} {
  return {
    // Required fields carry an aria-hidden asterisk, so the email field is found by role, whose
    // accessible name skips it; a password input has no role, so its label is matched by prefix.
    email: screen.getByRole<HTMLInputElement>('textbox', { name: 'Email' }),
    password: screen.getByLabelText<HTMLInputElement>(/^Password/u),
    submit: screen.getByRole<HTMLButtonElement>('button', { name: 'Sign in' }),
  };
}

/** The `<form>` that holds the controls, for a submission the disabled button cannot start. */
function loginForm(): HTMLFormElement {
  const form = controls().submit.closest('form');
  if (form === null) {
    throw new Error('The Sign in button is not inside a form.');
  }
  return form;
}

/** Types the credentials into the two fields and presses "Sign in". */
function signIn(email: string, password: string): void {
  const { email: emailField, password: passwordField, submit } = controls();
  fireEvent.change(emailField, { target: { value: email } });
  fireEvent.change(passwordField, { target: { value: password } });
  fireEvent.click(submit);
}

/** The texts the field's `aria-describedby` points at, in order. */
function descriptionOf(field: HTMLElement): string[] {
  const ids = (field.getAttribute('aria-describedby') ?? '').split(/\s+/u).filter(Boolean);
  return ids.map((id) => document.getElementById(id)?.textContent ?? `(missing #${id})`);
}

/** A response a test releases by hand, to hold the request pending in between. */
function heldResponse(): {
  promise: Promise<MockResponse>;
  release: (response: MockResponse) => void;
} {
  let release: (response: MockResponse) => void = () => undefined;
  const promise = new Promise<MockResponse>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('LoginPage', () => {
  it('renders the sign-in form under the product name and sends nothing on its own', async () => {
    const api = mockApi({});
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    expect(screen.getByRole('heading', { level: 1, name: 'Sign in' })).toBeTruthy();
    expect(screen.getByText('Ads Action Engine')).toBeTruthy();
    const { email, password, submit } = controls();
    expect(email.type).toBe('email');
    expect(email.name).toBe('email');
    expect(email.autocomplete).toBe('username');
    expect(email.required).toBe(true);
    expect(password.type).toBe('password');
    expect(password.name).toBe('password');
    expect(password.autocomplete).toBe('current-password');
    expect(password.required).toBe(true);
    expect(submit.type).toBe('submit');
    expect(submit.disabled).toBe(false);
    // The browser's own validation bubbles are off, so the page's messages are the only ones.
    expect(loginForm().noValidate).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it('shows a Primary navigation holding only the current Sign in link', async () => {
    const api = mockApi({});
    const { router } = await renderWithProviders(<LoginPage redirectTo="/decisions" />, {
      path: '/login?redirect=/decisions',
    });

    // Every screen has the side navigation (AAP 0.5.5); signed out, it lists no signed-in screen.
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((link) => link.textContent)).toEqual(['Sign in']);
    for (const name of ['Actions', 'Decisions', 'Sources', 'Settings']) {
      expect(screen.queryByRole('link', { name })).toBeNull();
    }
    const [link] = links;
    if (link === undefined) {
      throw new Error('The Primary navigation holds no link.');
    }
    expect(link.getAttribute('aria-current')).toBe('page');
    // The design-system class contract only: the router adds no "active" class of its own.
    expect(link.className).toBe('ds-sidenav__link ds-sidenav__link--current');
    // The link keeps the current search, so following it never drops the redirect.
    expect(link.getAttribute('href')).toBe('/login?redirect=%2Fdecisions');

    fireEvent.click(link);

    await waitFor(() => {
      expect(router.state.location.search).toEqual({ redirect: '/decisions' });
    });
    expect(router.state.location.pathname).toBe('/login');
    expect(api.calls).toHaveLength(0);
  });

  it('posts the trimmed email and the password as typed with the CSRF header', async () => {
    const api = mockApi({ 'POST /auth/login': json(200, makeMe()) });
    const { router } = await renderWithProviders(<LoginPage redirectTo="/decisions" />, {
      path: '/login?redirect=/decisions',
    });

    signIn(`  ${ADMIN_EMAIL}  `, TYPED_PASSWORD);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/decisions');
    });
    expect(api.calls).toHaveLength(1);
    const [call] = api.calls;
    expect(call?.method).toBe('POST');
    expect(call?.path).toBe('/auth/login');
    expect(call?.body).toEqual({ email: ADMIN_EMAIL, password: TYPED_PASSWORD });
    expect(call?.headers.get('X-Requested-With')).toBe('ads-engine');
    expect(call?.headers.get('Content-Type')).toBe('application/json');
  });

  it('seeds the signed-in user and replaces /login with the redirect path', async () => {
    mockApi({ 'POST /auth/login': json(200, makeMe()) });
    const { router, queryClient } = await renderWithProviders(
      <LoginPage redirectTo="/decisions" />,
      { path: '/login?redirect=/decisions' },
    );
    const entriesBefore = router.history.length;

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/decisions');
    });
    // Replaced, not pushed: Back does not return to the sign-in form.
    expect(router.history.length).toBe(entriesBefore);
    // The auth guard passes without another GET /auth/me.
    expect(queryClient.getQueryData<Me>(queryKeys.me)).toEqual(makeMe());
  });

  it('keeps the search of a same-origin redirect path', async () => {
    mockApi({ 'POST /auth/login': json(200, makeMe()) });
    // `/actions` keeps `run` only as a positive integer run id (as `src/router.tsx` does), so the
    // redirect carries a real run id rather than a free-form string such as `run-123`.
    const target = `/actions?run=${String(CUSTOM_RUN_ID)}`;
    const { router } = await renderWithProviders(<LoginPage redirectTo={target} />, {
      path: `/login?redirect=${encodeURIComponent(target)}`,
    });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/actions');
    });
    expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
  });

  it.each([
    ['a protocol-relative URL', '//evil.example/actions'],
    ['a backslash authority', '/\\evil.example/actions'],
    ['an absolute URL', 'https://evil.example/actions'],
    ['a script URL', 'javascript:alert(1)'],
    ['a path hiding a tab', '/\t/evil.example'],
    ['a path hiding a line break', '/\n/evil.example'],
    ['a path holding DEL', '/\u007f/evil.example'],
    ['a relative path', 'decisions'],
    ['an empty path', ''],
  ])('opens /actions instead of %s', async (_label, redirectTo) => {
    mockApi({ 'POST /auth/login': json(200, makeMe()) });
    const { router } = await renderWithProviders(<LoginPage redirectTo={redirectTo} />, {
      path: '/login',
    });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(router.state.location.href).toBe('/actions');
    });
  });

  it('submits on Enter: the fields and the submit button share one form', async () => {
    const api = mockApi({ 'POST /auth/login': json(200, makeMe()) });
    const { router } = await renderWithProviders(<LoginPage redirectTo="/actions" />, {
      path: '/login',
    });
    const { email, password, submit } = controls();

    // Enter in a text field submits natively when the field's form holds a submit button. jsdom
    // performs no implicit submission on an Enter keydown, so the structure that makes Enter
    // work is asserted, and the submission it triggers is dispatched on the form itself.
    const form = password.form;
    if (!(form instanceof HTMLFormElement)) {
      throw new Error('The Password field is not inside a form.');
    }
    expect(email.form).toBe(form);
    expect(submit.form).toBe(form);
    expect(submit.type).toBe('submit');

    fireEvent.change(email, { target: { value: ADMIN_EMAIL } });
    fireEvent.change(password, { target: { value: TYPED_PASSWORD } });
    fireEvent.submit(form);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/actions');
    });
    expect(api.calls).toHaveLength(1);
    expect(api.calls[0]?.method).toBe('POST');
    expect(api.calls[0]?.path).toBe('/auth/login');
    expect(api.calls[0]?.body).toEqual({ email: ADMIN_EMAIL, password: TYPED_PASSWORD });
  });

  it('reports both empty fields beside them, sends nothing and focuses the email field', async () => {
    const api = mockApi({});
    const { router } = await renderWithProviders(<LoginPage redirectTo="/actions" />, {
      path: '/login',
    });

    fireEvent.click(controls().submit);

    const { email, password } = controls();
    expect(descriptionOf(email)).toEqual(['Enter your email']);
    expect(email.getAttribute('aria-invalid')).toBe('true');
    expect(descriptionOf(password)).toEqual(['Enter your password']);
    expect(password.getAttribute('aria-invalid')).toBe('true');
    await waitFor(() => {
      expect(document.activeElement).toBe(email);
    });
    // Field messages only: no form banner, and no alert role (AAP 0.5.5).
    expect(screen.queryByRole('alert')).toBeNull();
    expect(api.calls).toHaveLength(0);
    expect(router.state.location.pathname).toBe('/login');
  });

  it('treats a whitespace-only email as empty', async () => {
    const api = mockApi({});
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn('   ', TYPED_PASSWORD);

    const { email, password } = controls();
    expect(descriptionOf(email)).toEqual(['Enter your email']);
    expect(email.getAttribute('aria-invalid')).toBe('true');
    expect(password.getAttribute('aria-invalid')).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(email);
    });
    expect(api.calls).toHaveLength(0);
  });

  it('with only the password filled, reports the email and focuses it', async () => {
    const api = mockApi({});
    const { router } = await renderWithProviders(<LoginPage redirectTo="/actions" />, {
      path: '/login',
    });

    signIn('', TYPED_PASSWORD);

    const { email, password } = controls();
    expect(descriptionOf(email)).toEqual(['Enter your email']);
    expect(email.getAttribute('aria-invalid')).toBe('true');
    expect(password.getAttribute('aria-invalid')).toBeNull();
    expect(screen.queryByText('Enter your password')).toBeNull();
    // The password stays as typed, ready for the corrected attempt.
    expect(password.value).toBe(TYPED_PASSWORD);
    await waitFor(() => {
      expect(document.activeElement).toBe(email);
    });
    expect(api.calls).toHaveLength(0);
    expect(router.state.location.pathname).toBe('/login');
  });

  it('with only the email filled, reports the password and focuses it', async () => {
    const api = mockApi({});
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, '');

    const { email, password } = controls();
    expect(email.getAttribute('aria-invalid')).toBeNull();
    expect(descriptionOf(password)).toEqual(['Enter your password']);
    await waitFor(() => {
      expect(document.activeElement).toBe(password);
    });
    expect(api.calls).toHaveLength(0);
  });

  it('clears a field message as soon as that field is edited', async () => {
    mockApi({});
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });
    fireEvent.click(controls().submit);

    fireEvent.change(controls().email, { target: { value: 'a' } });

    expect(screen.queryByText('Enter your email')).toBeNull();
    expect(screen.getByText('Enter your password')).toBeTruthy();

    fireEvent.change(controls().password, { target: { value: 'b' } });

    expect(screen.queryByText('Enter your password')).toBeNull();
  });

  it.each([
    ['no @', 'not-an-email'],
    ['nothing before the @', '@example.com'],
    ['nothing after the last @', 'admin@example.com@'],
    ['a space inside', 'ad min@example.com'],
    ['an em space inside', 'admin@exa\u2003mple.com'],
    ['a line separator inside', 'admin\u2028@example.com'],
  ])(
    'reports an email with %s under the email field and sends nothing',
    async (_label, typed) => {
      const api = mockApi({});
      await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

      signIn(typed, TYPED_PASSWORD);

      const { email, password } = controls();
      expect(descriptionOf(email)).toEqual([EMAIL_FORMAT_MESSAGE]);
      expect(email.getAttribute('aria-invalid')).toBe('true');
      expect(password.getAttribute('aria-invalid')).toBeNull();
      expect(password.value).toBe(TYPED_PASSWORD);
      await waitFor(() => {
        expect(document.activeElement).toBe(email);
      });
      expect(screen.queryByRole('alert')).toBeNull();
      expect(api.calls).toHaveLength(0);
    },
  );

  it.each([
    ['a second @ in the local part, split at the last @', 'first@last@example.com'],
    ['a domain without a dot', 'admin@localhost'],
    ['U+FEFF inside, which the API does not count as whitespace', 'ad\ufeffmin@example.com'],
  ])('sends an email with %s, which the API accepts as an admin email', async (_label, typed) => {
    const api = mockApi({
      'POST /auth/login': apiError(401, 'invalid_credentials', 'Invalid email or password'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(typed, TYPED_PASSWORD);

    await waitFor(() => {
      expect(api.calls).toHaveLength(1);
    });
    expect(api.calls[0]?.body).toEqual({ email: typed, password: TYPED_PASSWORD });
    expect(controls().email.getAttribute('aria-invalid')).toBeNull();
  });

  it('trims the email as the API trims it, including U+0085 and U+001F', async () => {
    const api = mockApi({ 'POST /auth/login': json(200, makeMe()) });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(`\u0085\u001f ${ADMIN_EMAIL}\u3000\t`, TYPED_PASSWORD);

    await waitFor(() => {
      expect(api.calls).toHaveLength(1);
    });
    expect(api.calls[0]?.body).toEqual({ email: ADMIN_EMAIL, password: TYPED_PASSWORD });
  });

  it('rejects an email of 321 code points after trimming and sends one of exactly 320', async () => {
    const api = mockApi({
      'POST /auth/login': apiError(401, 'invalid_credentials', 'Invalid email or password'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    // 309 + 12 = 321 code points once the surrounding ideographic spaces are trimmed. They are
    // not ASCII whitespace, so the page trims them, not the email input's value sanitisation.
    signIn(`\u3000${'a'.repeat(309)}${EMAIL_DOMAIN}\u3000`, TYPED_PASSWORD);

    const { email, password } = controls();
    expect(descriptionOf(email)).toEqual(['Email must be at most 320 characters']);
    expect(email.getAttribute('aria-invalid')).toBe('true');
    expect(password.getAttribute('aria-invalid')).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(email);
    });
    expect(api.calls).toHaveLength(0);

    // 308 astral characters are 616 UTF-16 units but 308 code points: 320 in all, the limit.
    const longest = `${'\u{1F600}'.repeat(308)}${EMAIL_DOMAIN}`;
    signIn(`\u3000${longest}\u3000`, TYPED_PASSWORD);

    await waitFor(() => {
      expect(api.calls).toHaveLength(1);
    });
    expect(api.calls[0]?.body).toEqual({ email: longest, password: TYPED_PASSWORD });
  });

  it('measures the email after lowercasing, as the API does', async () => {
    const api = mockApi({
      'POST /auth/login': apiError(401, 'invalid_credentials', 'Invalid email or password'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    // U+0130 lowercases to two code points: 154 typed become 308, plus 12 for the domain is 320.
    signIn(`${'\u0130'.repeat(154)}${EMAIL_DOMAIN}`, TYPED_PASSWORD);
    await waitFor(() => {
      expect(api.calls).toHaveLength(1);
    });

    // One more is 322 once lowercased, although only 167 code points are typed.
    signIn(`${'\u0130'.repeat(155)}${EMAIL_DOMAIN}`, TYPED_PASSWORD);

    expect(descriptionOf(controls().email)).toEqual(['Email must be at most 320 characters']);
    expect(api.calls).toHaveLength(1);
  });

  it('rejects a password of 1025 code points and sends 1024 astral characters unchanged', async () => {
    const api = mockApi({ 'POST /auth/login': json(200, makeMe()) });
    const { router } = await renderWithProviders(<LoginPage redirectTo="/actions" />, {
      path: '/login',
    });

    signIn(ADMIN_EMAIL, 'p'.repeat(1025));

    const { email, password } = controls();
    expect(descriptionOf(password)).toEqual(['Password must be at most 1024 characters']);
    expect(password.getAttribute('aria-invalid')).toBe('true');
    expect(email.getAttribute('aria-invalid')).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(password);
    });
    expect(api.calls).toHaveLength(0);

    // 1024 code points, each two UTF-16 units: within the limit, sent exactly as typed.
    const longest = '\u{1F511}'.repeat(1024);
    expect(longest).toHaveLength(2048);
    signIn(ADMIN_EMAIL, longest);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/actions');
    });
    expect(api.calls).toHaveLength(1);
    expect(api.calls[0]?.body).toEqual({ email: ADMIN_EMAIL, password: longest });
  });

  it('reports an invalid email and an empty password together and focuses the email', async () => {
    const api = mockApi({});
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn('not-an-email', '');

    const { email, password } = controls();
    expect(descriptionOf(email)).toEqual([EMAIL_FORMAT_MESSAGE]);
    expect(email.getAttribute('aria-invalid')).toBe('true');
    expect(descriptionOf(password)).toEqual(['Enter your password']);
    expect(password.getAttribute('aria-invalid')).toBe('true');
    await waitFor(() => {
      expect(document.activeElement).toBe(email);
    });
    expect(api.calls).toHaveLength(0);
  });

  it('shows a 422 naming body.email under the email field only', async () => {
    const detail = 'Value error, must not contain a NUL (U+0000) character';
    mockApi({
      'POST /auth/login': apiError(422, 'validation_error', `body.email: ${detail}`),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(descriptionOf(controls().email)).toEqual([detail]);
    });
    const { email, password } = controls();
    expect(email.getAttribute('aria-invalid')).toBe('true');
    expect(password.getAttribute('aria-invalid')).toBeNull();
    expect(descriptionOf(password)).toEqual([]);
    // A field message only: no banner, and nothing typed is cleared.
    expect(screen.getAllByText(detail)).toHaveLength(1);
    expect(password.value).toBe(TYPED_PASSWORD);
    expect(email.value).toBe(ADMIN_EMAIL);
    await waitFor(() => {
      expect(document.activeElement).toBe(email);
    });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows a 422 naming both fields under each and focuses the email', async () => {
    mockApi({
      'POST /auth/login': apiError(
        422,
        'validation_error',
        'body.password: String should have at most 1024 characters; ' +
          'body.email: Value error, a; b; body.extra: Extra inputs are not permitted (and 2 more)',
      ),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(descriptionOf(controls().email)).toEqual(['Value error, a; b']);
    });
    expect(descriptionOf(controls().password)).toEqual([
      'String should have at most 1024 characters',
    ]);
    await waitFor(() => {
      expect(document.activeElement).toBe(controls().email);
    });
  });

  it('shows a 422 naming only body.password under the password field and focuses it', async () => {
    mockApi({
      'POST /auth/login': apiError(
        422,
        'validation_error',
        'body.password: Input should be a valid string',
      ),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(descriptionOf(controls().password)).toEqual(['Input should be a valid string']);
    });
    expect(controls().email.getAttribute('aria-invalid')).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(controls().password);
    });
  });

  it('handles a 422 naming neither field like any other failure', async () => {
    mockApi({
      'POST /auth/login': apiError(422, 'validation_error', 'body: Field required'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    expect(await screen.findAllByText('body: Field required')).toHaveLength(2);
    const { email, password } = controls();
    expect(descriptionOf(password)).toEqual(['body: Field required']);
    expect(email.getAttribute('aria-invalid')).toBeNull();
    expect(password.value).toBe('');
    await waitFor(() => {
      expect(document.activeElement).toBe(password);
    });
  });

  it('shows one message for 401 invalid_credentials, clears the password and focuses it', async () => {
    mockApi({
      'POST /auth/login': apiError(401, 'invalid_credentials', 'Invalid email or password'),
    });
    const { router, queryClient } = await renderWithProviders(
      <LoginPage redirectTo="/actions" />,
      { path: '/login' },
    );

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    // The message heads the form and sits under the password field.
    const message = 'Email or password is incorrect';
    expect(await screen.findAllByText(message)).toHaveLength(2);
    const { email, password } = controls();
    expect(password.value).toBe('');
    expect(password.getAttribute('aria-invalid')).toBe('true');
    expect(descriptionOf(password)).toEqual([message]);
    await waitFor(() => {
      expect(document.activeElement).toBe(password);
    });
    expect(email.value).toBe(ADMIN_EMAIL);
    // The message concerns the attempt, not the email: the email field stays valid.
    expect(email.getAttribute('aria-invalid')).toBeNull();
    expect(descriptionOf(email)).toEqual([]);
    // The page never says which part failed and never echoes the password.
    expect(screen.queryByText('Invalid email or password')).toBeNull();
    expect(document.body.textContent).not.toContain(TYPED_PASSWORD.trim());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(router.state.location.pathname).toBe('/login');
    expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
  });

  it.each([
    ['30', 'Too many attempts. Try again in 30 seconds'],
    ['1', 'Too many attempts. Try again in 1 second'],
    ['0', 'Too many attempts. Try again shortly.'],
    ['soon', 'Too many attempts. Try again shortly.'],
  ])(
    'shows the wait of 429 rate_limited with Retry-After %j',
    async (retryAfter, expected) => {
      mockApi({
        'POST /auth/login': apiError(429, 'rate_limited', 'Too many login attempts', {
          'Retry-After': retryAfter,
        }),
      });
      await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

      signIn(ADMIN_EMAIL, TYPED_PASSWORD);

      expect(await screen.findAllByText(expected)).toHaveLength(2);
      expect(descriptionOf(controls().password)).toEqual([expected]);
      expect(screen.queryByText('Too many login attempts')).toBeNull();
    },
  );

  it('says "shortly" for 429 rate_limited without a Retry-After', async () => {
    mockApi({
      'POST /auth/login': apiError(429, 'rate_limited', 'Too many login attempts'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    expect(await screen.findAllByText('Too many attempts. Try again shortly.')).toHaveLength(2);
    expect(controls().password.value).toBe('');
  });

  it("shows the API's own message for any other refusal, under the password too", async () => {
    mockApi({
      'POST /auth/login': apiError(403, 'csrf_header_missing', 'Missing X-Requested-With header'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    const message = 'Missing X-Requested-With header';
    expect(await screen.findAllByText(message)).toHaveLength(2);
    expect(descriptionOf(controls().password)).toEqual([message]);
    expect(controls().password.value).toBe('');
  });

  /** Case, the stub that makes the attempt fail, and the message the page shows for it. */
  const OUTAGES: readonly (readonly [string, () => void, string])[] = [
    [
      "a 503 with the API's envelope",
      () => {
        mockApi({
          'POST /auth/login': apiError(503, 'database_unavailable', 'Database unavailable'),
        });
      },
      'Database unavailable',
    ],
    [
      "a 502 error page without the API's envelope",
      () => {
        mockApi({ 'POST /auth/login': html(502, '<html><body>Bad gateway</body></html>') });
      },
      'The server is temporarily unavailable. Try again shortly.',
    ],
    [
      'a request that never reached the server',
      () => {
        mockApi({});
        vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
      },
      'The server could not be reached. Check your connection and try again.',
    ],
  ];

  it.each(OUTAGES)(
    'shows %s once, at the head of the form, and keeps what was typed',
    async (_case, stub, message) => {
      stub();
      await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });
      // The polite region exists, empty, before the first attempt, so its message is announced.
      const region = screen.getByRole('status');
      expect(region.childNodes).toHaveLength(0);

      signIn(ADMIN_EMAIL, TYPED_PASSWORD);

      const shown = await screen.findAllByText(message);
      expect(shown).toHaveLength(1);
      expect(region.contains(shown[0] ?? null)).toBe(true);
      expect(screen.getByRole('status')).toBe(region);
      // The API never judged the credentials: no field is invalid or described by the message,
      // and nothing typed is cleared.
      const { email, password, submit } = controls();
      expect(password.getAttribute('aria-invalid')).toBeNull();
      expect(descriptionOf(password)).toEqual([]);
      expect(email.getAttribute('aria-invalid')).toBeNull();
      expect(descriptionOf(email)).toEqual([]);
      expect(password.value).toBe(TYPED_PASSWORD);
      expect(email.value).toBe(ADMIN_EMAIL);
      // Focus was on <body>, off the busy button; it returns to "Sign in", where Enter retries.
      await waitFor(() => {
        expect(document.activeElement).toBe(submit);
      });
      expect(submit.disabled).toBe(false);
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it('leaves focus in the password field after an outage submitted from it', async () => {
    mockApi({ 'POST /auth/login': html(502, '<html><body>Bad gateway</body></html>') });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });
    const { email, password } = controls();
    fireEvent.change(email, { target: { value: ADMIN_EMAIL } });
    fireEvent.change(password, { target: { value: TYPED_PASSWORD } });

    // Enter pressed in the password field: the form submits with focus in that field.
    password.focus();
    fireEvent.submit(loginForm());

    await screen.findByText('The server is temporarily unavailable. Try again shortly.');
    await waitFor(() => {
      expect(controls().submit.disabled).toBe(false);
    });
    expect(document.activeElement).toBe(password);
    expect(password.value).toBe(TYPED_PASSWORD);
    expect(password.getAttribute('aria-invalid')).toBeNull();
  });

  it('clears an outage message while the retry is pending and shows what that attempt gets', async () => {
    const api = mockApi({
      'POST /auth/login': apiError(503, 'database_unavailable', 'Database unavailable'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });
    signIn(ADMIN_EMAIL, TYPED_PASSWORD);
    await screen.findByText('Database unavailable');
    const region = screen.getByRole('status');

    const retry = heldResponse();
    api.use({ 'POST /auth/login': () => retry.promise });
    fireEvent.click(controls().submit);

    // The region empties while the retry runs, so a repeated outage would be new content.
    await waitFor(() => {
      expect(region.childNodes).toHaveLength(0);
    });
    retry.release(apiError(401, 'invalid_credentials', 'Invalid email or password'));

    expect(await screen.findAllByText('Email or password is incorrect')).toHaveLength(2);
    expect(screen.queryByText('Database unavailable')).toBeNull();
    expect(region.childNodes).toHaveLength(0);
    expect(controls().password.value).toBe('');
    // The retry sent the password the outage had kept, exactly as typed.
    expect(api.calls.map((call) => call.body)).toEqual([
      { email: ADMIN_EMAIL, password: TYPED_PASSWORD },
      { email: ADMIN_EMAIL, password: TYPED_PASSWORD },
    ]);
  });

  it('is busy while the request is pending and ignores a second submission', async () => {
    const response = heldResponse();
    const api = mockApi({ 'POST /auth/login': () => response.promise });
    const { router } = await renderWithProviders(<LoginPage redirectTo="/actions" />, {
      path: '/login',
    });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(controls().submit.getAttribute('aria-busy')).toBe('true');
    });
    const { email, password, submit } = controls();
    // Inert but never natively disabled, so a focused Sign in keeps focus: a second press is
    // cancelled.
    expect(submit.getAttribute('aria-disabled')).toBe('true');
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    // The fields stay editable while the request runs.
    expect(email.disabled).toBe(false);
    expect(password.disabled).toBe(false);
    // A submit event reaching the form directly is a no-op too while the request is pending.
    fireEvent.submit(loginForm());
    expect(api.calls).toHaveLength(1);

    response.release(json(200, makeMe()));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/actions');
    });
    expect(api.calls).toHaveLength(1);
    expect(controls().submit.disabled).toBe(false);
    expect(controls().submit.getAttribute('aria-busy')).toBeNull();
  });

  it('moves focus back to the password field after a failure submitted from it', async () => {
    mockApi({
      'POST /auth/login': apiError(401, 'invalid_credentials', 'Invalid email or password'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });
    signIn(ADMIN_EMAIL, TYPED_PASSWORD);
    await screen.findAllByText('Email or password is incorrect');
    await waitFor(() => {
      expect(document.activeElement).toBe(controls().password);
    });

    // Enter pressed in the focused password field: the form submits with focus already there.
    const { password } = controls();
    fireEvent.change(password, { target: { value: 'still wrong' } });
    fireEvent.submit(loginForm());

    await waitFor(() => {
      expect(controls().password.value).toBe('');
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(controls().password);
    });
    expect(screen.getAllByText('Email or password is incorrect')).toHaveLength(2);
  });

  it('clears the previous failure when a new attempt succeeds', async () => {
    const api = mockApi({
      'POST /auth/login': apiError(401, 'invalid_credentials', 'Invalid email or password'),
    });
    const { router } = await renderWithProviders(<LoginPage redirectTo="/decisions" />, {
      path: '/login?redirect=/decisions',
    });
    signIn(ADMIN_EMAIL, 'wrong');
    await screen.findAllByText('Email or password is incorrect');

    api.use({ 'POST /auth/login': json(200, makeMe()) });
    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/decisions');
    });
    expect(screen.queryByText('Email or password is incorrect')).toBeNull();
    expect(controls().password.getAttribute('aria-invalid')).toBeNull();
    expect(api.calls.map((call) => call.body)).toEqual([
      { email: ADMIN_EMAIL, password: 'wrong' },
      { email: ADMIN_EMAIL, password: TYPED_PASSWORD },
    ]);
  });
});
