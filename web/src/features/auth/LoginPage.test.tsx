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
 * - validation of empty fields, with nothing sent and focus on the first empty field;
 * - 401 `invalid_credentials`, 429 `rate_limited` with and without a usable `Retry-After`, any
 *   other API failure, and the pending state that blocks a second submission.
 *
 * @packageDocumentation
 */
import { apiError, html, json, mockApi } from '../../test/mockApi';
import type { MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

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

  it("shows the API's own message for any other API failure", async () => {
    mockApi({
      'POST /auth/login': apiError(503, 'database_unavailable', 'Database unavailable'),
    });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    expect(await screen.findAllByText('Database unavailable')).toHaveLength(2);
    expect(controls().password.value).toBe('');
  });

  it("shows the client's message for an error page without the API's envelope", async () => {
    mockApi({ 'POST /auth/login': html(502, '<html><body>Bad gateway</body></html>') });
    await renderWithProviders(<LoginPage redirectTo="/actions" />, { path: '/login' });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    expect(
      await screen.findAllByText('The server is temporarily unavailable. Try again shortly.'),
    ).toHaveLength(2);
  });

  it('is busy while the request is pending and ignores a second submission', async () => {
    const response = heldResponse();
    const api = mockApi({ 'POST /auth/login': () => response.promise });
    const { router } = await renderWithProviders(<LoginPage redirectTo="/actions" />, {
      path: '/login',
    });

    signIn(ADMIN_EMAIL, TYPED_PASSWORD);

    await waitFor(() => {
      expect(controls().submit.disabled).toBe(true);
    });
    const { email, password, submit } = controls();
    expect(submit.getAttribute('aria-busy')).toBe('true');
    // The fields stay editable while the request runs.
    expect(email.disabled).toBe(false);
    expect(password.disabled).toBe(false);
    // Enter in a field still submits the form; the pending request makes it a no-op.
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
