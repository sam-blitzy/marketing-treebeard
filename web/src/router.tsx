/**
 * The SPA's route tree, auth guard and authenticated app layout (AAP 0.5.5 routes; AAP 0.1.1
 * "The UI requires a login"; AAP 0.5.3 every endpoint except login and healthz needs the session).
 *
 * Routes, all code-based TanStack Router routes (no plugin, no file-based routing, no codegen):
 *
 * | Path         | Screen           | Guard and search                                        |
 * |--------------|------------------|---------------------------------------------------------|
 * | `/login`     | `LoginPage`      | signed out; `?redirect=` only as a same-origin path     |
 * | `/`          | none             | redirects to `/actions`                                 |
 * | `/actions`   | `ActionsPage`    | signed in; `?run=<id>` addresses one run                |
 * | `/decisions` | `DecisionsPage`  | signed in                                               |
 * | `/sources`   | `SourcesPage`    | signed in                                               |
 * | `/settings`  | `SettingsPage`   | signed in (read-only)                                   |
 * | any other    | "Page not found" | signed in, so it renders inside the app shell           |
 *
 * `/login` serves signed-out visitors only: one already signed in is sent on, replacing the
 * entry, to the `redirect` path (search and hash included) or to `/actions`, the default page
 * after login (see `loginRoute`). Every visit to `/login` revalidates the session, through the
 * cached user or one `GET /auth/me`, so a tab still holding a 401 from before a sign-in in
 * another tab of the same browser is sent on too. The one exception is the arrival of the auth
 * guard's own 401 redirect, which the guard marks in its history state (`authGuardSignedOut`):
 * the guard has just asked, so `/login` takes that answer once for that history entry and asks
 * nothing (see {@link isGuardSignedOutArrival}).
 *
 * `/login` receives two things besides that mark: the `redirect` search, and, when an open
 * screen's request found the session ended, the reason in its history state (`sessionEnded`,
 * written by `src/main.tsx`). `LoginPage` shows the reason as its "Your session ended" notice. It
 * travels in the history state, not the URL, so no link can make the screen claim that an action
 * was lost; the guard's own 401 redirect writes none, so a first visit while signed out shows no
 * notice.
 *
 * `LoginPage` and `ActionsPage` are imported statically; the `/decisions`, `/sources` and
 * `/settings` screens are code-split (see "Code splitting" below).
 *
 * The four signed-in screens sit under one pathless layout route, `authenticated`. Its
 * `beforeLoad` reads `GET /auth/me` through the TanStack Query cache
 * (`ensureQueryData(meQueryOptions())`), which keeps the user for the life of the tab, so
 * navigation between screens re-asks nothing while the cached user is present, however long ago
 * it was read. A 401 redirects to `/login?redirect=<the requested href>`, marked as the guard's
 * redirect in its history state. Any other failure (for example 503 `database_unavailable`, or a
 * 502 while the API is down) leaves the session unknown, never mistaken for a signed-out one: the
 * layout renders the requested page, whose own failure state explains the outage and reads again
 * by itself, and the top bar shows the email once `GET /auth/me` answers (see {@link AppLayout}).
 * No data can appear without a session: every page read needs the session cookie, and a 401
 * from any of them opens `/login` (`src/main.tsx`).
 *
 * The layout ({@link AppLayout}) renders the one `PageShell` of every signed-in screen through
 * {@link SignedInShell}: the `TopBar` with the signed-in email (once known) and "Sign out", the
 * primary `SideNav` (Actions, Decisions, Sources, Settings) and the routed page in `<main>`. The
 * layout's own error and pending screens ({@link SignedInError}, {@link SignedInPending}) use
 * the same shell without the email and render no page: the pending screen waits for the guard,
 * and the error screen stands in for a layout that failed to render.
 * Each page renders its own `PageShell.Title` and sections, and `LoginPage` renders its own
 * `PageShell`, so every screen has exactly one `<main>` landmark.
 *
 * Error, not-found and pending screens render through {@link RouteFrame}: inside a signed-in
 * shell they fill `<main>` beside the primary navigation, and outside it (the login screen
 * failed or is still pending, the root route failed, or a `notFound()` was raised outside the
 * layout) they bring a {@link SignInShell}: a `PageShell` with the product's `TopBar` and the
 * one-link "Sign in" `SideNav` that `LoginPage` shows, so every screen has a `PageShell`, a
 * `TopBar` and a `SideNav` (AAP 0.5.5) and exactly one `<main>`. The error and not-found
 * screens render a `PageShell.Title`, the page's one `<h1>`; the pending screen renders only its
 * labelled `Spinner`, with no heading.
 *
 * Document titles (WCAG 2.2 SC 2.4.2 Page Titled): every screen with a `PageShell.Title` titles
 * the document "{title} · Ads Action Engine" through `useDocumentTitle` (`./lib/documentTitle`),
 * each page from its own module and {@link RootError} and {@link NotFound} here; the pending
 * screen sets none, so the previous screen's title stays until the next screen renders.
 *
 * Code splitting (AAP 0.1.1: the five sections render from precomputed runs in under 2 s): the
 * entry bundle carries only the screens a first load can show, `/actions` (the landing page)
 * and `/login`. `DecisionsPage`, `SourcesPage` and `SettingsPage` are `lazyRouteComponent`s over
 * a dynamic `import()`, so each screen's page module, with the modules only that screen reaches,
 * forms a chunk that a first `/actions` or `/login` load neither downloads nor parses. Modules a
 * screen shares with the entry bundle stay in that bundle: `/actions` imports `DecisionForm` and
 * `DiscrepancyResolver` too, so the `/decisions` chunk reuses them from it. The chunk loads:
 * - **On intent**: `defaultPreload: 'intent'` preloads a route, chunk included, when its link is
 *   hovered, focused or touched, so a click usually finds the screen loaded.
 * - **On navigation** otherwise: the route stays pending until its chunk has loaded, and a load
 *   slower than the router's pending threshold (`defaultPendingMs`, 1 s) shows
 *   {@link RoutePending} in the layout's `<main>`, beside the navigation.
 * - **On failure**: the import rejects with a {@link ScreenChunkError} and the screen throws it
 *   when it renders. For a missing module, as after a deploy that replaced the hashed chunks
 *   (nginx answers a missing `/assets/` file with 404), TanStack Router reloads the page once
 *   per error message in the tab's session (sessionStorage key
 *   `tanstack_router_reload:<message>`), which fetches the new `index.html`. Any other failure,
 *   or the same one again, shows {@link RootError} in the layout. Its Retry reloads the page,
 *   because the browser keeps a failed module fetch for the life of the document.
 *
 * Focus after navigation (WCAG 2.2 SC 2.4.3) is handled once, by the always-mounted root route:
 * see {@link useFocusRecoveryAfterNavigation} for route navigations and
 * {@link useFocusOnHashTarget} for in-page links.
 *
 * Rules this file keeps:
 * - **Server state only through `./api/queries`** (AAP 0.11.1). The CSRF header on
 *   `POST /auth/logout` is added by the API client; nothing here fetches.
 * - **Design system only** (AAP 0.4.4), imported from its index: no raw interactive or heading
 *   elements, no `style`, no stylesheet. Router links are TanStack `Link`s rendered through
 *   `SideNav`'s `renderLink`, so they carry the design-system class contract.
 * - **No alert role** (AAP 0.5.5 reserves `role="alert"` for open discrepancies).
 * - **No cycle**: features never import this file. They type `Link` and `useNavigate` through the
 *   `Register` augmentation at the end of this module.
 * - **Lean entry bundle** (AAP 0.1.1): `DecisionsPage`, `SourcesPage` and `SettingsPage` are
 *   never imported statically, here or by anything the entry imports, or their graphs fold back
 *   into the entry chunk.
 *
 * @example
 * ```tsx
 * // src/main.tsx
 * const queryClient = new QueryClient();
 * const router = createAppRouter(queryClient);
 * createRoot(container).render(
 *   <QueryClientProvider client={queryClient}>
 *     <RouterProvider router={router} />
 *   </QueryClientProvider>,
 * );
 *
 * // A test, on its own memory history:
 * const router = createAppRouter(queryClient, {
 *   history: createMemoryHistory({ initialEntries: ['/actions?run=202'] }),
 * });
 * ```
 *
 * @packageDocumentation
 */
import { useQueryClient } from '@tanstack/react-query';
import type { QueryClient } from '@tanstack/react-query';
import {
  Link,
  Outlet,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  lazyRouteComponent,
  redirect,
  useNavigate,
  useRouter,
  useRouterState,
} from '@tanstack/react-router';
import type { ErrorComponentProps, ParsedLocation, RouterHistory } from '@tanstack/react-router';
import { createContext, useContext, useEffect, useId, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import { isApiError, meQueryOptions, useLogout, useMe } from './api/queries';
import type { ApiError } from './api/queries';
import type { Me, RunId } from './api/types';
import {
  Banner,
  Button,
  EmptyState,
  Inline,
  PageShell,
  SideNav,
  Spinner,
  Stack,
  TopBar,
} from './design-system';
import type { SideNavItem, SideNavLinkProps } from './design-system';
import { ActionsPage } from './features/actions/ActionsPage';
import { LoginPage } from './features/auth/LoginPage';
import type { SessionEndedNotice } from './features/auth/LoginPage';
import { useDocumentTitle } from './lib/documentTitle';
import { focusElement, idFromHash } from './lib/focus';

// ---------------------------------------------------------------------------------------------
// Texts and constants
// ---------------------------------------------------------------------------------------------

/** The product name in every top bar. */
const PRODUCT_NAME = 'Ads Action Engine';

/** Where a sign-in goes when `/login` carries no usable `redirect` (AAP 0.5.5 default page). */
const DEFAULT_AFTER_LOGIN = '/actions';

/** Accessible name of the primary navigation landmark. */
const PRIMARY_NAV_LABEL = 'Primary';

/**
 * The sign-in screen's path: the one destination of the navigation a screen outside the
 * authenticated layout shows ({@link SignInShell}).
 */
const SIGN_IN_PATH = '/login';

/** Text of that navigation's one link, the sign-in screen's own title. */
const SIGN_IN_NAV_LABEL = 'Sign in';

/** Text of the top bar's sign-out button. */
const SIGN_OUT_LABEL = 'Sign out';

/** Shown beside "Sign out" when `POST /auth/logout` failed and the session is still valid. */
const SIGN_OUT_FAILED_MESSAGE = 'Sign-out failed. Try again.';

/** Title of every error screen, rendered as its `<h1>`. */
const ERROR_TITLE = 'Something went wrong';

/** What an error screen says when the failure carries no message safe to show. */
const GENERIC_ERROR_MESSAGE = 'The page could not be loaded. Try again.';

/** Label of the error screen's retry button. */
const RETRY_LABEL = 'Retry';

/** Title of the not-found screen, rendered as its `<h1>`. */
const NOT_FOUND_TITLE = 'Page not found';

/** What the not-found screen says below its title. */
const NOT_FOUND_REASON = 'No page exists at this address.';

/** Text of the not-found screen's link back to the default page. */
const NOT_FOUND_ACTION = 'Go to actions';

/** Accessible label of the spinner shown while a route resolves. */
const PENDING_LABEL = 'Loading';

/** The paths of the primary navigation; each is a route of the tree below. */
type PrimaryPath = '/actions' | '/decisions' | '/sources' | '/settings';

/** One destination of the primary navigation. */
interface PrimaryNavEntry {
  /** Stable key, unique within the navigation. */
  readonly key: string;
  /** Visible link text. */
  readonly label: string;
  /** The route the link opens. */
  readonly to: PrimaryPath;
}

/**
 * The primary navigation, in the order AAP 0.5.5 lists the screens. It carries no counts:
 * section counts belong to the `/actions` section navigation (`features/actions/SectionNav`).
 */
const PRIMARY_NAV: readonly PrimaryNavEntry[] = Object.freeze([
  { key: 'actions', label: 'Actions', to: '/actions' },
  { key: 'decisions', label: 'Decisions', to: '/decisions' },
  { key: 'sources', label: 'Sources', to: '/sources' },
  { key: 'settings', label: 'Settings', to: '/settings' },
]);

/**
 * `activeProps` for the navigation's router links. TanStack `Link` adds an `active` class to an
 * active link unless `activeProps` is given; an empty object keeps the class list exactly the
 * `SideNav` contract (`ds-sidenav__link`, plus `--current` from the item's `current` flag).
 */
const NO_LINK_STATE_PROPS = Object.freeze({});

/**
 * `activeOptions` for the navigation's router links: `Link` adds `aria-current="page"` to an
 * active link by itself, so it must judge "active" exactly as {@link isCurrentPath} does (same
 * path, search ignored). Its default, a path prefix, would mark Actions on `/actions/nope`.
 */
const PRIMARY_LINK_ACTIVE_OPTIONS = Object.freeze({ exact: true, includeSearch: false });

/** Highest code point of the ASCII C0 control range, U+0000–U+001F. */
const C0_CONTROL_MAX = 0x1f;

/** Code point of DELETE, U+007F. */
const DELETE_CODE = 0x7f;

// ---------------------------------------------------------------------------------------------
// Search parsing
// ---------------------------------------------------------------------------------------------

/** The search parameters of `/login`. */
interface LoginSearch {
  /** The same-origin path to open after sign-in, such as `/actions?run=202`. */
  redirect?: string;
}

/** The search parameters of `/actions`. */
interface ActionsSearch {
  /** The run to show; absent for the latest default run (AAP 0.5.3 run selection). */
  run?: RunId;
}

/**
 * Whether `value` holds an ASCII control character. Browsers drop tabs and newlines inside a
 * URL, so `/\t/evil.example` would be read as the protocol-relative `//evil.example`.
 *
 * @param value - The candidate path.
 * @returns `true` when any code unit is U+0000–U+001F or U+007F.
 */
function hasControlCharacter(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code <= C0_CONTROL_MAX || code === DELETE_CODE) {
      return true;
    }
  }
  return false;
}

/**
 * The `redirect` search value when it is a same-origin path, otherwise `undefined`, so an
 * attacker-supplied link cannot send a fresh sign-in off-site (open redirect).
 *
 * A same-origin path starts with exactly one `/`. Rejected: anything that is not a string, a
 * value without the leading `/` (a scheme such as `https:` or `javascript:`, or a relative
 * path), a protocol-relative `//host`, `/\host` (browsers read the backslash as a slash) and any
 * value holding a control character. `LoginPage` repeats the check before it navigates.
 *
 * @param value - The raw `redirect` search value, as the router parsed it.
 * @returns The path, unchanged, or `undefined` when it is dropped.
 */
function parseRedirect(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }
  const sameOrigin =
    value.startsWith('/') &&
    !value.startsWith('//') &&
    !value.startsWith('/\\') &&
    !hasControlCharacter(value);
  return sameOrigin ? value : undefined;
}

/**
 * The `run` search value as a {@link RunId}, otherwise `undefined`, so an invalid value shows the
 * default run instead of an error.
 *
 * `Run.id` is a positive integer (`analysis_run.id`). The router's default parser turns
 * `?run=202` into the number `202`; a quoted `?run="202"` arrives as a string of digits and is
 * accepted too. Zero, negatives, fractions, values beyond `Number.MAX_SAFE_INTEGER` (which would
 * lose precision) and every other value are dropped.
 *
 * @param value - The raw `run` search value, as the router parsed it.
 * @returns The run id, or `undefined` when it is dropped.
 */
function parseRunId(value: unknown): RunId | undefined {
  const candidate =
    typeof value === 'number'
      ? value
      : typeof value === 'string' && /^[1-9][0-9]*$/u.test(value)
        ? Number(value)
        : undefined;
  return candidate !== undefined && Number.isSafeInteger(candidate) && candidate > 0
    ? candidate
    : undefined;
}

/**
 * `validateSearch` of `/login`: keeps `redirect` only when {@link parseRedirect} accepts it.
 *
 * The key is always returned, `undefined` when dropped. TanStack Router merges a route's
 * validated search over the raw search it parsed from the URL, so a key left out of the result
 * would keep its raw, unsafe value in `useSearch()`; an explicit `undefined` replaces it.
 *
 * @param search - The parsed search object.
 * @returns `{ redirect }`, with `redirect` `undefined` when it is absent or unsafe.
 */
function validateLoginSearch(search: Record<string, unknown>): LoginSearch {
  return { redirect: parseRedirect(search.redirect) };
}

/**
 * `validateSearch` of `/actions`: keeps `run` only when {@link parseRunId} accepts it.
 *
 * The key is always returned, `undefined` when dropped, for the reason given at
 * {@link validateLoginSearch}: an omitted key would keep its raw value.
 *
 * @param search - The parsed search object.
 * @returns `{ run }`, with `run` `undefined` when it is absent or invalid.
 */
function validateActionsSearch(search: Record<string, unknown>): ActionsSearch {
  return { run: parseRunId(search.run) };
}

/**
 * Whether the URL's raw search carries `key` although the route's `validateSearch` dropped its
 * value. The route then redirects, replacing the history entry, to the same path without it,
 * so the address bar, a reload and a bookmark never keep the rejected value (for example an
 * off-site `redirect`) and `location.search` agrees with what the screen uses.
 *
 * @param rawSearch - `location.search`: the search as parsed from the URL, before validation.
 * @param key - The search key the route validates.
 * @param validated - The value `validateSearch` returned for `key`.
 * @returns `true` when the URL holds a value for `key` that validation rejected.
 */
function carriesRejectedValue(rawSearch: object, key: string, validated: unknown): boolean {
  return validated === undefined && key in rawSearch;
}

// ---------------------------------------------------------------------------------------------
// Code-split screens
// ---------------------------------------------------------------------------------------------

/**
 * The chunk of a code-split screen failed to load: a deploy replaced the hashed chunk, or the
 * network failed. It keeps the failure's message, so `lazyRouteComponent` still recognises a
 * missing module and reloads the page once, and carries the failure itself as `cause`.
 *
 * {@link RootError} tells it apart from every other failure. The browser keeps a failed module
 * fetch for the life of the document, so re-running the import fails again at once; Retry
 * therefore reloads the page for this error instead of invalidating the router.
 */
class ScreenChunkError extends Error {
  /** Always `'ScreenChunkError'`, so the class survives minification. */
  override readonly name = 'ScreenChunkError';

  /**
   * @param cause - What the screen's `import()` rejected with.
   */
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
  }
}

/**
 * Wraps the `import()` of a code-split screen so that a failure rejects with a
 * {@link ScreenChunkError}. A successful import resolves to the module unchanged, so
 * `lazyRouteComponent` keeps the module's type and checks the export name against it.
 *
 * @param importer - The screen's `import()`, with a literal specifier so Vite splits it out.
 * @returns The importer to hand to `lazyRouteComponent`.
 */
function screenImporter<TModule>(importer: () => Promise<TModule>): () => Promise<TModule> {
  return () =>
    importer().catch((cause: unknown) => {
      throw new ScreenChunkError(cause);
    });
}

// ---------------------------------------------------------------------------------------------
// Shared frame of the error, not-found and pending screens
// ---------------------------------------------------------------------------------------------

/**
 * `true` inside {@link SignedInShell}, whose `PageShell` already provides the top bar, the
 * navigation and `<main>`: the authenticated layout and its own error and pending screens.
 * {@link RouteFrame} reads it to decide whether to bring a `PageShell` of its own.
 */
const InsideAppLayoutContext = createContext(false);

/** Props of {@link RouteFrame}. */
interface RouteFrameProps {
  /**
   * The screen's content: the title slot and body of an error or not-found screen, or the
   * labelled `Spinner` of a pending screen.
   */
  children: ReactNode;
}

/**
 * The frame of a screen the router renders on its own: an error, a not-found path or a pending
 * route.
 *
 * Inside a {@link SignedInShell} (the authenticated layout, or its own error and pending
 * screens) the content fills that shell's `<main>`, next to the primary navigation. Outside it
 * (the login screen failed or is still pending, the root route failed, or a `notFound()` was
 * raised outside the layout) the content gets a {@link SignInShell}, so the screen still has a
 * `TopBar`, a `SideNav`, exactly one `<main>` and a skip link. Either way the content is one
 * `Stack`, spaced like the pages.
 */
function RouteFrame({ children }: RouteFrameProps): ReactElement {
  const insideAppLayout = useContext(InsideAppLayoutContext);
  const content = <Stack gap={6}>{children}</Stack>;
  if (insideAppLayout) {
    return content;
  }
  return <SignInShell>{content}</SignInShell>;
}

/**
 * `SideNav`'s `renderLink` for the sign-in navigation of {@link SignInShell}: a TanStack `Link`
 * to `/login` carrying the class contract and `aria-current` that `SideNav` built, as
 * `LoginPage` renders its own. On `/login` itself (its item is current) it keeps the current
 * search, so activating it never drops the `redirect` a sign-in returns to; from any other path
 * it opens `/login` without a search. It judges "active" as {@link isCurrentPath} does, so
 * `Link` never adds an `aria-current` of its own that the item does not carry.
 *
 * @param item - The item being rendered; its destination is also `linkProps.href`.
 * @param linkProps - The props `SideNav` built for the link.
 * @returns The link element.
 */
function renderSignInLink(item: SideNavItem, linkProps: SideNavLinkProps): ReactNode {
  return (
    <Link
      to={SIGN_IN_PATH}
      search={item.current === true ? true : {}}
      className={linkProps.className}
      aria-current={linkProps['aria-current']}
      activeProps={NO_LINK_STATE_PROPS}
      activeOptions={PRIMARY_LINK_ACTIVE_OPTIONS}
    >
      {linkProps.children}
    </Link>
  );
}

/** Props of {@link SignInShell}. */
interface SignInShellProps {
  /** What `<main>` holds: the content of an error, not-found or pending screen. */
  children: ReactNode;
}

/**
 * The `PageShell` of a screen the router renders outside the authenticated layout: the login
 * route's error and pending screens, the root route's error screen, and a `notFound()` raised
 * outside the layout.
 *
 * Every screen uses `PageShell`, `TopBar` and `SideNav` (AAP 0.5.5), so it brings all three: the
 * `TopBar` with the product name only, and the "Primary" `SideNav` that `LoginPage` shows,
 * holding one link, "Sign in", to `/login`, marked current only on `/login`. Nothing here knows
 * whether the visitor is signed in, so the navigation never lists the signed-in screens, as
 * `LoginPage`'s never does.
 */
function SignInShell({ children }: SignInShellProps): ReactElement {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const items: SideNavItem[] = [
    {
      key: 'login',
      label: SIGN_IN_NAV_LABEL,
      href: SIGN_IN_PATH,
      current: isCurrentPath(pathname, SIGN_IN_PATH),
    },
  ];
  return (
    <PageShell
      topBar={<TopBar productName={PRODUCT_NAME} />}
      sideNav={<SideNav label={PRIMARY_NAV_LABEL} items={items} renderLink={renderSignInLink} />}
    >
      {children}
    </PageShell>
  );
}

/**
 * Moves focus to the page's `<main>` when a navigation or reload has removed the focused control,
 * so keyboard and screen-reader users continue in the new screen's content instead of starting
 * over from `<body>` (WCAG 2.2 SC 2.4.3 Focus Order).
 *
 * Examples: "Back to default" on a custom run, whose banner unmounts with the link; "Sign in",
 * whose form gives way to the app; "Sign out", whose top bar gives way to the sign-in form;
 * "Retry" on the error screen, which the reload replaces with the pending and then the result
 * screen. `PageShell` makes `<main>` focusable (`tabindex="-1"`), and focus moves without
 * scrolling.
 *
 * It listens to the router's `onRendered` event, which fires once a load (a navigation, or the
 * `router.invalidate()` behind Retry) has committed its screen, so the old screen and its
 * focused control are gone by then. It never acts on the app's initial load, which has no
 * previous location (focus then starts on `<body>`, with the skip link as the first Tab stop),
 * and it never takes focus from a control that is still mounted, such as a navigation link the
 * user activated.
 *
 * It never acts on a hash-only navigation either (same path and search, another hash), such as
 * an in-page link to a section: that stays on the same screen, and
 * {@link useFocusOnHashTarget} moves focus to the element the hash names. The browser leaves
 * focus on `<body>` after following a link to an element that is not focusable, so without this
 * exception `<main>` would take focus and the next Tab would start over at the top of the page.
 *
 * Called from the root route's component, which stays mounted across every navigation, so it
 * also covers a change of layout (sign-in, sign-out).
 */
function useFocusRecoveryAfterNavigation(): void {
  const router = useRouter();

  useEffect(
    () =>
      router.subscribe('onRendered', ({ fromLocation, toLocation }) => {
        // No previous location, or the router re-announcing the location it mounted with: the
        // initial load, where focus is left on <body>.
        if (fromLocation === undefined || fromLocation === toLocation) {
          return;
        }
        // A hash-only navigation keeps the screen: its focus belongs to the hash target.
        if (
          toLocation.pathname === fromLocation.pathname &&
          toLocation.searchStr === fromLocation.searchStr &&
          toLocation.hash !== fromLocation.hash
        ) {
          return;
        }
        const focused = document.activeElement;
        if (focused === null || focused === document.body) {
          document.querySelector('main')?.focus({ preventScroll: true });
        }
      }),
    [router],
  );
}

/**
 * The fragment of the same-document link a click follows, such as `#section-keywords` of a
 * section navigation link or `#group-campaigns-discrepancies` of the discrepancy banner's link,
 * or `null` when the click follows no such link.
 *
 * A click qualifies only when the browser will follow the link in this document: no handler
 * prevented its default (a router `Link` does, and so does the skip link, which focuses
 * `<main>` itself), the primary button with no modifier key (modifiers open a new tab or window,
 * or download), no `download` attribute and no browsing context other than this one (`target`,
 * or the page's `<base target>`). Its URL must be this document's own origin, path and search
 * with a non-empty fragment; a bare `#` scrolls to the top and names no element.
 *
 * @param event - a `click` that has reached `document`, after every handler of the app
 * @returns the link's fragment with its `#`, as `URL.hash` spells it, or `null`
 */
function sameDocumentLinkHash(event: MouseEvent): string | null {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey
  ) {
    return null;
  }
  const link = event.target instanceof Element ? event.target.closest('a[href]') : null;
  if (!(link instanceof HTMLAnchorElement) || link.hasAttribute('download')) {
    return null;
  }
  const context =
    link.getAttribute('target') ??
    link.ownerDocument.querySelector('base[target]')?.getAttribute('target') ??
    '';
  if (context !== '' && context.toLowerCase() !== '_self') {
    return null;
  }
  let url: URL;
  try {
    url = new URL(link.href);
  } catch {
    // An href the URL parser rejects is one the browser does not follow either.
    return null;
  }
  const here = window.location;
  const sameDocument =
    url.origin === here.origin && url.pathname === here.pathname && url.search === here.search;
  return sameDocument && url.hash !== '' ? url.hash : null;
}

/**
 * Moves focus to the element an in-page link names, so keyboard and screen-reader users
 * continue inside the section, group or card they asked for (WCAG 2.2 SC 2.4.3 Focus Order):
 * after a link such as a section navigation link (`#section-{key}`), the discrepancy banner's
 * "Review discrepancies" (`#group-campaigns-discrepancies`) or a link to a card (`#item-{id}`),
 * focus is on that element, and the next Tab reaches the first control inside it.
 *
 * The browser follows the link itself (it changes the hash and scrolls the target into view),
 * but it moves focus only to a target that is focusable. The regions and cards these links name
 * are not, so it would leave focus on `<body>`. The element therefore gets `tabindex="-1"` for as
 * long as it holds focus ({@link focusElement}), and focus moves without scrolling, because the
 * browser has scrolled already.
 *
 * - **Click** (with the mouse, or Enter on a focused link): on a click on a same-document link
 *   ({@link sameDocumentLinkHash}), the target is focused at once, before the browser follows
 *   the link, so the browser's own focus step finds it focusable and keeps focus there. A link
 *   to the hash already in the URL fires no `hashchange`, so this alone covers activating the
 *   same link again.
 * - **`hashchange`**: back and forward through in-page entries, or an edited address, focus the
 *   element the new hash names.
 *
 * A hash that names no element leaves focus where it is. A target the page has not rendered
 * yet, such as a card beyond the shown batch, is revealed, scrolled to and focused by its list
 * on `hashchange` (`features/actions/batches.tsx`), and a held card behind its collapsed
 * disclosure by that disclosure (`HeldDisclosure`, `features/actions/sections/SectionFrame.tsx`).
 * A page load with a hash moves no focus.
 *
 * Called from the root route's component, which stays mounted on every screen.
 */
function useFocusOnHashTarget(): void {
  useEffect(() => {
    const focusTarget = (hash: string): void => {
      const id = idFromHash(hash);
      const element = id === null ? null : document.getElementById(id);
      if (element !== null) {
        focusElement(element, { preventScroll: true });
      }
    };
    const onClick = (event: MouseEvent): void => {
      const hash = sameDocumentLinkHash(event);
      if (hash !== null) {
        focusTarget(hash);
      }
    };
    const onHashChange = (): void => {
      focusTarget(window.location.hash);
    };
    // Bubble phase on document, so the app's own handlers, which React runs on its root
    // container, have run and any of them may have prevented the default.
    document.addEventListener('click', onClick);
    window.addEventListener('hashchange', onHashChange);
    return () => {
      document.removeEventListener('click', onClick);
      window.removeEventListener('hashchange', onHashChange);
    };
  }, []);
}

/**
 * The root route's component: the matched child route and nothing else (the signed-in frame is
 * {@link AppLayout}'s, and `/login` renders its own), plus focus recovery after navigation and
 * focus on the target of an in-page link.
 */
function RootComponent(): ReactElement {
  useFocusRecoveryAfterNavigation();
  useFocusOnHashTarget();
  return <Outlet />;
}

/**
 * The error screen of every route: the root route's `errorComponent` and the router's
 * `defaultErrorComponent`, and the content of the authenticated layout's own error screen
 * ({@link SignedInError}), which renders it inside a {@link SignedInShell}.
 *
 * It shows the "Something went wrong" title, then a critical `Banner` whose title is the
 * failure's message: the {@link ApiError} message, which the API client guarantees is safe to
 * show (for example "Database unavailable" for a 503 `database_unavailable`), or a generic
 * sentence for anything else. A stack trace or an internal error text is never shown. The banner
 * has no `role="alert"` (AAP 0.5.5).
 *
 * "Retry" invalidates the router, which re-runs every matched route's `beforeLoad` (the auth
 * guard reads `GET /auth/me` again unless the cache holds the user), and resets the error
 * boundary so the route renders again. For a {@link ScreenChunkError} it reloads the page
 * instead: the browser keeps the failed module fetch for the life of the document, so only a
 * new document can load the screen's chunk.
 *
 * The title carries "Something went wrong" and the banner the specific message, so each text
 * appears once on the screen and the page keeps one `<h1>` (UI1: one `<h1>` per page). The
 * document is titled "Something went wrong · Ads Action Engine" ({@link useDocumentTitle}).
 */
function RootError({ error, reset }: ErrorComponentProps): ReactElement {
  useDocumentTitle(ERROR_TITLE);
  const router = useRouter();
  const message = isApiError(error) && error.message !== '' ? error.message : GENERIC_ERROR_MESSAGE;

  const retry = () => {
    if (error instanceof ScreenChunkError) {
      window.location.reload();
      return;
    }
    void router.invalidate();
    reset();
  };

  return (
    <RouteFrame>
      <PageShell.Title>{ERROR_TITLE}</PageShell.Title>
      <Banner
        tone="critical"
        title={message}
        actions={
          <Button variant="primary" onClick={retry}>
            {RETRY_LABEL}
          </Button>
        }
      />
    </RouteFrame>
  );
}

/**
 * The not-found screen: the component of the catch-all route under the authenticated layout,
 * and the root route's `notFoundComponent` and the router's `defaultNotFoundComponent` for a
 * `notFound()` raised outside it. It shows the "Page not found" title and an `EmptyState` with a
 * router link to `/actions`, the default page.
 *
 * The title is the page's single `<h1>` (UI1) and titles the document "Page not found · Ads
 * Action Engine" ({@link useDocumentTitle}); the empty state states why the page is empty and
 * offers the way back.
 */
function NotFound(): ReactElement {
  useDocumentTitle(NOT_FOUND_TITLE);
  return (
    <RouteFrame>
      <PageShell.Title>{NOT_FOUND_TITLE}</PageShell.Title>
      <EmptyState
        title={NOT_FOUND_REASON}
        action={
          <Link to="/actions" search={{}}>
            {NOT_FOUND_ACTION}
          </Link>
        }
      />
    </RouteFrame>
  );
}

/**
 * The router's `defaultPendingComponent`: a design-system `Spinner` labelled "Loading", shown
 * while a route's `beforeLoad` (the auth guard's first `GET /auth/me`) or a code-split screen's
 * chunk takes longer than the router's pending threshold.
 *
 * It renders no heading and sets no document title: the previous screen's title (or, on a first
 * load, the boot document's "Ads Action Engine") stays until the screen being loaded renders and
 * sets its own.
 */
function RoutePending(): ReactElement {
  return (
    <RouteFrame>
      <Spinner label={PENDING_LABEL} showLabel />
    </RouteFrame>
  );
}

// ---------------------------------------------------------------------------------------------
// Authenticated layout
// ---------------------------------------------------------------------------------------------

/**
 * Whether `href` is one of the primary navigation's paths, so it can be handed to the typed
 * TanStack `Link` without a cast.
 *
 * @param href - A `SideNav` link's validated destination.
 * @returns `true` for a {@link PrimaryPath}.
 */
function isPrimaryPath(href: string): href is PrimaryPath {
  return PRIMARY_NAV.some((entry) => entry.to === href);
}

/**
 * Whether the navigation destination `to` is the screen at `pathname`.
 *
 * The match is exact, ignoring one trailing slash: the screens have no sub-pages, so a path
 * below one (`/actions/nope`) is the not-found screen and marks no link as the current page. The
 * search is ignored, so `/actions?run=202`, a custom run, still marks Actions, and
 * `/login?redirect=…` still marks "Sign in".
 *
 * @param pathname - The router's current pathname.
 * @param to - A primary destination, or the sign-in screen of {@link SignInShell}.
 * @returns `true` when the navigation should mark `to` as the current page.
 */
function isCurrentPath(pathname: string, to: PrimaryPath | typeof SIGN_IN_PATH): boolean {
  const normalised =
    pathname.length > 1 && pathname.endsWith('/') ? pathname.slice(0, -1) : pathname;
  return normalised === to;
}

/**
 * `SideNav`'s `renderLink` for the primary navigation: a TanStack `Link`, so navigation is
 * client-side, carrying the class contract and `aria-current` that `SideNav` built. No `style`
 * is passed. A destination outside {@link PRIMARY_NAV} cannot occur, because the items are built
 * from it; it would render the plain anchor `SideNav` itself renders.
 *
 * @param _item - The item being rendered; its destination is also `linkProps.href`.
 * @param linkProps - The props `SideNav` built for the link.
 * @returns The link element.
 */
function renderPrimaryLink(_item: SideNavItem, linkProps: SideNavLinkProps): ReactNode {
  const { href, className, children } = linkProps;
  const ariaCurrent = linkProps['aria-current'];
  if (!isPrimaryPath(href)) {
    return (
      <a href={href} className={className} aria-current={ariaCurrent}>
        {children}
      </a>
    );
  }
  return (
    <Link
      to={href}
      className={className}
      aria-current={ariaCurrent}
      activeProps={NO_LINK_STATE_PROPS}
      activeOptions={PRIMARY_LINK_ACTIVE_OPTIONS}
    >
      {children}
    </Link>
  );
}

/**
 * Whether a failed sign-out still ends the session in the UI: a 401 means the session had
 * already expired, so the user is signed out either way.
 *
 * @param error - The logout mutation's error.
 * @returns `true` for a 401.
 */
function isSessionGone(error: ApiError): boolean {
  return error.status === 401;
}

/** Props of {@link SignedInShell}. */
interface SignedInShellProps {
  /**
   * The signed-in email the top bar shows. Omitted on the authenticated layout's own error and
   * pending screens, and by the layout while the session is unknown (the API could not answer
   * `GET /auth/me`); an empty email is not shown either.
   */
  email?: string;
  /** What `<main>` holds: the routed page, or the layout's error or pending content. */
  children: ReactNode;
}

/**
 * The one `PageShell` of every signed-in screen, and of the authenticated layout's own error and
 * pending screens. It provides `InsideAppLayoutContext`, so a {@link RouteFrame} inside it fills
 * its `<main>` instead of bringing a shell of its own.
 *
 * - **Top bar**: the product name, the signed-in email when `email` is given (plain text, not a
 *   heading) and a quiet "Sign out" button.
 * - **Sign out** posts `/auth/logout`. Once it succeeds, or fails with 401 because the session
 *   had already ended, the query cache is cleared and the screen moves to `/login`, where
 *   {@link useFocusRecoveryAfterNavigation} places focus. Any other failure keeps the user signed
 *   in and shows "Sign-out failed. Try again." beside the button, linked to it through
 *   `aria-describedby` while it shows.
 *   - **Announcement** (WCAG 2.2 SC 4.1.3): the message sits in a polite `role="status"`
 *     region that mounts with the layout and holds nothing else, so each failure adds the
 *     message to a region assistive technology already watches; the next attempt removes it
 *     while pending, so a repeated failure is announced again. The button sits outside the
 *     region, so no control is inside a live region and a change of its label or busy state is
 *     never announced as status; with the message alone inside, the region needs no
 *     `aria-atomic`. The region is never `role="alert"`, which open discrepancies alone use.
 *     An `Inline`, spaced like the top bar's actions row, lays out the region and then the
 *     button on one row. `Inline` takes the region out of flow while it is empty, so without a
 *     failure the row holds the button alone and the bar looks as it would without the region.
 *   - **Focus** (WCAG 2.2 SC 2.4.3): the loading button keeps focus (it is `aria-disabled`,
 *     never natively disabled), but focus can still be lost to `<body>` while the request is
 *     pending. After each failure, once the render that re-enables the button has committed,
 *     focus returns to "Sign out" when it is on `<body>` or nowhere. Focus the user moved to
 *     another control during the request stays where it is.
 * - **Primary navigation**: Actions, Decisions, Sources and Settings, in that order. The current
 *   screen's link carries `aria-current="page"`, set by `SideNav` from the router's pathname.
 * - **Main**: `children`.
 *
 * It reads no server state: the mutation behind "Sign out" sends nothing until it is used.
 *
 * @param props - See {@link SignedInShellProps}.
 * @returns The shell around `children`.
 */
function SignedInShell({ email, children }: SignedInShellProps): ReactElement {
  const logout = useLogout();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const signOutErrorId = useId();
  const signOutButtonRef = useRef<HTMLButtonElement>(null);
  // The number of sign-outs that failed and kept the session. Every failure changes it, so the
  // effect below runs once per failure, after the render that shows the message has committed.
  const [signOutFailures, setSignOutFailures] = useState(0);

  useEffect(() => {
    if (signOutFailures === 0) {
      return;
    }
    const focused = document.activeElement;
    if (focused === null || focused === document.body) {
      signOutButtonRef.current?.focus();
    }
  }, [signOutFailures]);

  const signOut = () => {
    logout.mutate(undefined, {
      onSettled: (_data, error) => {
        if (error === null || isSessionGone(error)) {
          queryClient.clear();
          void navigate({ to: '/login' });
          return;
        }
        // Focusing here would reach the button while it is still disabled. The render this
        // update schedules reads the settled mutation, so it re-enables the button before the
        // effect above runs.
        setSignOutFailures((count) => count + 1);
      },
    });
  };

  const signOutFailed = logout.error !== null && !isSessionGone(logout.error);

  const items: SideNavItem[] = PRIMARY_NAV.map((entry) => ({
    key: entry.key,
    label: entry.label,
    href: entry.to,
    current: isCurrentPath(pathname, entry.to),
  }));

  return (
    <InsideAppLayoutContext.Provider value={true}>
      <PageShell
        topBar={
          <TopBar productName={PRODUCT_NAME}>
            {email !== undefined && email !== '' && <span>{email}</span>}
            <Inline gap={3} justify="end">
              <div role="status" aria-live="polite">
                {signOutFailed && <span id={signOutErrorId}>{SIGN_OUT_FAILED_MESSAGE}</span>}
              </div>
              <Button
                ref={signOutButtonRef}
                variant="quiet"
                loading={logout.isPending}
                aria-describedby={signOutFailed ? signOutErrorId : undefined}
                onClick={signOut}
              >
                {SIGN_OUT_LABEL}
              </Button>
            </Inline>
          </TopBar>
        }
        sideNav={<SideNav label={PRIMARY_NAV_LABEL} items={items} renderLink={renderPrimaryLink} />}
      >
        {children}
      </PageShell>
    </InsideAppLayoutContext.Provider>
  );
}

/**
 * The authenticated layout: the {@link SignedInShell} of every signed-in screen, with the
 * signed-in email, around the routed page, or "Page not found" for a path no screen matches.
 *
 * The email is the user the auth guard read from `GET /auth/me` (or sign-in seeded) and put in
 * the route context. When the guard could not confirm the session (a 5xx or no network: the
 * context holds no user), the routed page renders all the same and its own failure state
 * explains the outage, while the top bar shows no email and {@link useMe} reads the user again:
 * every `CURRENT_STATE_POLL_MS` while the read fails, and when the window regains focus. Its
 * first answer settles the session:
 *
 * - **200**: the top bar shows the email, and the layout keeps it while it stays mounted. Nothing
 *   reloads, so focus stays where it is; the next navigation's guard takes the user from the
 *   cache, sending nothing.
 * - **401**: the layout re-runs the guard (`router.invalidate()`), which asks once more and
 *   redirects to `/login` exactly as on any other 401, its history mark included, because
 *   `src/main.tsx` leaves the 401 of this read to the guard.
 *
 * The observer is enabled only while no email is known, so once the context or that read has
 * given the user it sends nothing: sign-out clears the query cache before the screen leaves, and
 * an enabled observer would then re-fetch `GET /auth/me` only to receive the 401 of the session
 * just ended.
 */
function AppLayout(): ReactElement {
  const router = useRouter();
  const confirmedEmail = authenticatedRoute.useRouteContext({
    select: (context) => context.me?.email,
  });
  // The email the layout's own read returned while the guard could not confirm the session.
  const [readEmail, setReadEmail] = useState<string | undefined>(undefined);
  const email = confirmedEmail ?? readEmail;
  const me = useMe({ enabled: email === undefined });
  if (email === undefined && me.data !== undefined) {
    // React's pattern for adjusting state from what a render reads: the update made during
    // render re-renders this component at once, before its children, with the email kept, so
    // the observer is disabled before a later cache clear could make it read again.
    setReadEmail(me.data.email);
  }
  const sessionEnded = email === undefined && me.error?.status === 401;

  useEffect(() => {
    if (sessionEnded) {
      void router.invalidate();
    }
  }, [sessionEnded, router]);

  return (
    <SignedInShell email={email}>
      <Outlet />
    </SignedInShell>
  );
}

/**
 * The authenticated layout's `errorComponent`: the error screen of a failure of the layout's own
 * match, such as its component failing to render. Its auth guard throws nothing but the redirect
 * of a 401, and a routed screen's failure (a code-split screen's {@link ScreenChunkError}
 * included) is shown by that screen's own route, inside the layout. Its content,
 * {@link RootError}'s "Something went wrong", safe message and Retry, fills the `<main>` of a
 * {@link SignedInShell}, beside the primary navigation and below "Sign out", as on every
 * signed-in screen. The top bar shows no email, because the layout that holds it is what failed.
 *
 * @param props - The router's error props, passed on to {@link RootError}.
 * @returns The error screen inside the signed-in shell.
 */
function SignedInError(props: ErrorComponentProps): ReactElement {
  return (
    <SignedInShell>
      <RootError {...props} />
    </SignedInShell>
  );
}

/**
 * The authenticated layout's `pendingComponent`: while its auth guard waits for `GET /auth/me`
 * longer than the router's pending threshold, {@link RoutePending}'s spinner fills the `<main>`
 * of a {@link SignedInShell} without an email, so the screen is framed as it will be once the
 * guard passes. No page renders or reads data before it has.
 *
 * @returns The pending screen inside the signed-in shell.
 */
function SignedInPending(): ReactElement {
  return (
    <SignedInShell>
      <RoutePending />
    </SignedInShell>
  );
}

// ---------------------------------------------------------------------------------------------
// Route tree
// ---------------------------------------------------------------------------------------------

/**
 * What every route receives as `context`: the app's one `QueryClient`, so the auth guard reads
 * `GET /auth/me` through the same cache the hooks use.
 */
export interface RouterContext {
  /** The app's TanStack Query client (`src/main.tsx`). */
  queryClient: QueryClient;
}

/** The root route: renders the matched child, with the app's error and not-found screens. */
const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: RootComponent,
  errorComponent: RootError,
  notFoundComponent: NotFound,
});

/**
 * The `/login` history entries, per query client and by their `__TSR_key`, whose session check
 * the auth guard's 401 has already stood in for once (see {@link isGuardSignedOutArrival}).
 */
const guardAnsweredEntries = new WeakMap<QueryClient, Set<string>>();

/**
 * Whether this `/login` load is the arrival of the auth guard's own 401 redirect, so the
 * guard's answer stands in for the session check and the API is not asked twice in a row.
 *
 * All three must hold:
 * - The entry carries `state.authGuardSignedOut`, which only that redirect writes (see
 *   {@link authenticatedRoute}).
 * - The `queryKeys.me` entry still holds a 401, the answer the guard redirected on, so nothing
 *   has confirmed a session since.
 * - This history entry has not been answered this way before. The marker stays on its entry,
 *   so any later load of it (Back or Forward to it, Retry on its error screen) asks the API, as
 *   every other visit does: by then the visitor may have signed in in another tab, which shares
 *   this tab's session cookie. A reload starts with an empty cache, so it asks too.
 *
 * The test is deterministic: it reads the history entry and the cache, never the clock.
 *
 * @param queryClient - The app's query client, from the route context.
 * @param state - The history state of the `/login` location being loaded.
 * @returns `true` at most once per marked entry, and only while the guard's 401 is cached.
 */
function isGuardSignedOutArrival(
  queryClient: QueryClient,
  state: ParsedLocation['state'],
): boolean {
  const entry = state.__TSR_key;
  if (state.authGuardSignedOut !== true || entry === undefined) {
    return false;
  }
  const cached = queryClient.getQueryState(meQueryOptions().queryKey);
  if (cached?.status !== 'error' || !isApiError(cached.error) || cached.error.status !== 401) {
    return false;
  }
  const answered = guardAnsweredEntries.get(queryClient) ?? new Set<string>();
  if (answered.has(entry)) {
    return false;
  }
  answered.add(entry);
  guardAnsweredEntries.set(queryClient, answered);
  return true;
}

/**
 * Whether the visitor opening `/login` already holds a valid session, read through the
 * `queryKeys.me` entry the auth guard reads (`meQueryOptions()`).
 *
 * - The auth guard's own 401 redirect arriving ({@link isGuardSignedOutArrival}): `false`, with
 *   no new request. The guard has just read `GET /auth/me` and the API answered 401.
 * - Every other visit: `ensureQueryData` answers, from the cache when it holds the user (seeded
 *   by sign-in or read by the guard), else with one `GET /auth/me`, which never retries. A
 *   cached 401 holds no user, so the "Sign in" links, an in-app navigation to `/login`, and
 *   Back or Forward to a `/login` entry all ask the API again, and find a session started in
 *   another tab. Sign-out and an expired session clear the cache before they open `/login`, and
 *   a reload starts with an empty one, so those ask as well.
 *   - 200 `{email}`: `true`.
 *   - Any failure (a 401, a 5xx, no response at all): `false`. No session can be confirmed, so
 *     the sign-in form shows, and the form reports an outage itself when the visitor submits.
 *
 * @param queryClient - The app's query client, from the route context.
 * @param state - The history state of the `/login` location being loaded.
 * @returns `true` when the API confirmed the session, or the cache holds the signed-in user.
 */
async function hasSession(
  queryClient: QueryClient,
  state: ParsedLocation['state'],
): Promise<boolean> {
  if (isGuardSignedOutArrival(queryClient, state)) {
    return false;
  }
  try {
    await queryClient.ensureQueryData(meQueryOptions());
    return true;
  } catch {
    // Whatever the failure, nothing confirmed a session: the sign-in form is the screen to show.
    return false;
  }
}

/**
 * `/login`, outside the authenticated layout, for signed-out visitors. Its `beforeLoad`:
 *
 * 1. Removes a rejected `redirect` from the URL (see {@link carriesRejectedValue}).
 * 2. Stops there on a preload (a hovered or focused "Sign in" link, `defaultPreload: 'intent'`),
 *    so intent alone never sends a request.
 * 3. Sends a visitor already signed in ({@link hasSession}) on to the sanitised `redirect`, as an
 *    `href`, so its search and hash survive (`/actions?run=202`), or to `/actions`, the default
 *    page after login. The redirect replaces `/login` in the history, so Back does not return to
 *    it. Every other visitor gets the sign-in form.
 *
 * Every visit revalidates the session, so a tab still holding a 401 from before a sign-in in
 * another tab finds that session the next time `/login` opens. The one load that asks nothing is
 * the arrival of the auth guard's own 401 redirect, recognised by the `authGuardSignedOut` mark
 * the guard puts in its history state, because the guard has just asked
 * ({@link isGuardSignedOutArrival}).
 *
 * The redirect is thrown after {@link hasSession} has settled, outside its `try`, so no catch
 * can swallow it. It cannot loop: `src/main.tsx` leaves the `queryKeys.me` read's 401 to the
 * routes and clears the cache before it opens `/login` for any other 401, so the check asks
 * the API again, which answers 401. A `redirect` that is itself a `/login` URL is followed the
 * same way, from the cached user, and each hop drops one level of nesting, so the chain ends at
 * the innermost target, or at `/actions` when that has none.
 *
 * The screen ({@link LoginRouteComponent}) receives the sanitised `redirect` and, from the
 * location's history state, the `sessionEnded` reason `src/main.tsx` wrote when an open screen's
 * request was answered 401 ({@link sessionEndedNoticeOf}). Neither changes what `beforeLoad`
 * does: a visitor whose session holds again is sent on whatever the reason says.
 */
const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  validateSearch: validateLoginSearch,
  beforeLoad: async ({ context, location, preload, search }): Promise<void> => {
    if (carriesRejectedValue(location.search, 'redirect', search.redirect)) {
      throw redirect({ to: '/login', search: {}, replace: true });
    }
    if (preload || !(await hasSession(context.queryClient, location.state))) {
      return;
    }
    throw redirect({ href: search.redirect ?? DEFAULT_AFTER_LOGIN, replace: true });
  },
  component: LoginRouteComponent,
});

/**
 * The session-ended reason a `/login` history entry carries, when it is one `LoginPage` knows.
 *
 * The browser keeps an entry's state across reloads and across versions of the app, so the
 * value is checked rather than trusted: anything but `'session_ended'` or `'action_not_saved'`
 * reads as no reason.
 *
 * @param state - The history state of the current location.
 * @returns The reason, or `undefined` when the entry carries none.
 */
function sessionEndedNoticeOf(state: ParsedLocation['state']): SessionEndedNotice | undefined {
  const value: unknown = state.sessionEnded;
  return value === 'session_ended' || value === 'action_not_saved' ? value : undefined;
}

/**
 * The `/login` screen with its sanitised redirect target, `/actions` by default, and the
 * session-ended reason of its history entry, if any ({@link sessionEndedNoticeOf}).
 */
function LoginRouteComponent(): ReactElement {
  const search = loginRoute.useSearch();
  const sessionEnded = useRouterState({
    select: (state) => sessionEndedNoticeOf(state.location.state),
  });
  return (
    <LoginPage redirectTo={search.redirect ?? DEFAULT_AFTER_LOGIN} sessionEnded={sessionEnded} />
  );
}

/** `/`: always redirects to `/actions`, whose guard sends a signed-out user to `/login`. */
const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  beforeLoad: (): never => {
    throw redirect({ to: '/actions' });
  },
});

/** What the auth guard adds to the context of the signed-in routes. */
interface AuthenticatedContext {
  /**
   * The signed-in user, as `GET /auth/me` (or sign-in) returned it; `undefined` while the
   * session is unknown, because the API could not answer (a 5xx, no network).
   */
  me: Me | undefined;
}

/**
 * The pathless layout of the signed-in screens, with the auth guard.
 *
 * `ensureQueryData` resolves from the cache when `queryKeys.me` holds the user (seeded by
 * sign-in, or read once by an earlier navigation, and kept for the life of the tab) and
 * otherwise calls `GET /auth/me`, which never retries. The user is returned as route context
 * (`me`), which the layout's top bar shows. A 401 becomes a redirect to `/login` carrying the
 * requested href (path, search and hash) as `redirect`, so sign-in returns there, and the
 * `authGuardSignedOut` mark in its history state, so `/login` takes that fresh 401 as its own
 * check instead of asking again ({@link isGuardSignedOutArrival}).
 *
 * Every other failure (another `ApiError`, such as a 502 or 503 while the API is down or a
 * `network_error`, or any other rejection) means the session is unknown, not ended: the guard
 * returns no user, and the layout renders the requested page, whose own failure state shows the
 * outage and reads again by itself, rather than sending a signed-in user to the login form or
 * replacing the page with an error screen. That stays safe: every page read needs the session
 * cookie, and a 401 from any of them opens `/login` (`src/main.tsx`). The layout reads the user
 * again until the API answers, and re-runs this guard on a 401 ({@link AppLayout}); the next
 * navigation asks again too, because the failed read left no user in the cache. While the
 * guard waits longer than the router's pending threshold, {@link SignedInPending} shows the
 * spinner in the signed-in shell.
 */
const authenticatedRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'authenticated',
  beforeLoad: async ({ context, location }): Promise<AuthenticatedContext> => {
    try {
      const me = await context.queryClient.ensureQueryData(meQueryOptions());
      return { me };
    } catch (error) {
      if (isApiError(error) && error.status === 401) {
        throw redirect({
          to: '/login',
          search: { redirect: location.href },
          state: { authGuardSignedOut: true },
        });
      }
      // Session unknown: an explicit `undefined`, so no user from an earlier load survives in
      // the merged route context.
      return { me: undefined };
    }
  },
  component: AppLayout,
  errorComponent: SignedInError,
  pendingComponent: SignedInPending,
});

/**
 * `/actions`, the default page after login; `?run=<id>` addresses one run. An invalid `run` is
 * removed from the URL (see {@link carriesRejectedValue}), so the default run shows.
 */
const actionsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/actions',
  validateSearch: validateActionsSearch,
  beforeLoad: ({ location, search }): void => {
    if (carriesRejectedValue(location.search, 'run', search.run)) {
      throw redirect({ to: '/actions', search: {}, replace: true });
    }
  },
  component: ActionsRouteComponent,
});

/** The `/actions` screen for the run in the search, or the latest default run without one. */
function ActionsRouteComponent(): ReactElement {
  const { run } = actionsRoute.useSearch();
  return <ActionsPage runId={run} />;
}

/**
 * `/decisions`: discrepancy resolution and the decision log. Code-split: `DecisionsPage` is a
 * separate chunk, loaded on link intent or first visit (see the module header).
 */
const decisionsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/decisions',
  component: lazyRouteComponent(
    screenImporter(() => import('./features/decisions/DecisionsPage')),
    'DecisionsPage',
  ),
});

/**
 * `/sources`: per-source mode, health and freshness, and "Sync now". Code-split: `SourcesPage`
 * is a separate chunk, loaded on link intent or first visit (see the module header).
 */
const sourcesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/sources',
  component: lazyRouteComponent(
    screenImporter(() => import('./features/sources/SourcesPage')),
    'SourcesPage',
  ),
});

/**
 * `/settings`: the read-only configuration view. Code-split: `SettingsPage` is a separate
 * chunk, loaded on link intent or first visit (see the module header).
 */
const settingsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/settings',
  component: lazyRouteComponent(
    screenImporter(() => import('./features/settings/SettingsPage')),
    'SettingsPage',
  ),
});

/**
 * The catch-all (`$`) under the authenticated layout: every path no other route matches, such as
 * `/no-such-page` or `/actions/nope`.
 *
 * TanStack Router's fuzzy not-found matching does not enter a pathless layout, so without this
 * route an unknown path would render the root not-found screen outside the app shell, without
 * the navigation and "Sign out". Under the layout it passes the auth guard first: a signed-in
 * user sees "Page not found" inside the shell, and a signed-out user is sent to sign in, which
 * also means the app never tells a signed-out visitor which paths exist. Static routes always
 * outrank a splat, so `/`, `/login` and the four screens are unaffected.
 */
const catchAllRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '$',
  component: NotFound,
});

/**
 * The whole tree: `/login` and `/` beside the authenticated layout with its four screens and the
 * catch-all.
 */
const routeTree = rootRoute.addChildren([
  loginRoute,
  indexRoute,
  authenticatedRoute.addChildren([
    actionsRoute,
    decisionsRoute,
    sourcesRoute,
    settingsRoute,
    catchAllRoute,
  ]),
]);

// ---------------------------------------------------------------------------------------------
// Router factory
// ---------------------------------------------------------------------------------------------

/**
 * Builds the router over {@link routeTree}. Kept separate from {@link createAppRouter} so
 * {@link AppRouter} is inferred from the call itself and carries the full route tree type.
 *
 * Every route without its own screens uses {@link RootError}, {@link NotFound} and
 * {@link RoutePending}, so no TanStack default (with its raw button and stack details) ever
 * renders. Links preload on intent: the auth guard, which reads the cached user, and the chunk of
 * a code-split screen not loaded yet.
 *
 * @param queryClient - The app's query client, passed to every route as context.
 * @param history - The history to run on; the browser history when omitted.
 * @returns The router.
 */
function buildRouter(queryClient: QueryClient, history: RouterHistory | undefined) {
  return createRouter({
    routeTree,
    context: { queryClient },
    history,
    defaultPreload: 'intent',
    defaultPendingComponent: RoutePending,
    defaultErrorComponent: RootError,
    defaultNotFoundComponent: NotFound,
  });
}

/**
 * The app's router type, carrying the full route tree, so `Link`, `useNavigate`, `redirect` and
 * `useSearch` are type-checked against the real paths and search parameters.
 */
export type AppRouter = ReturnType<typeof buildRouter>;

/** Options of {@link createAppRouter}. */
export interface CreateAppRouterOptions {
  /**
   * The history to run on. Omitted, the router uses the browser history; tests pass
   * `createMemoryHistory({ initialEntries: [path] })`.
   */
  history?: RouterHistory;
}

/**
 * Creates the app's router: the route tree above with `queryClient` as every route's context.
 *
 * Create one per app (and one per test). Render it inside the `QueryClientProvider` of the same
 * client, because the layout's hooks read that provider while the guard reads the context.
 *
 * @param queryClient - The app's TanStack Query client.
 * @param options - Optional history; see {@link CreateAppRouterOptions}.
 * @returns The router, ready for `<RouterProvider router={router} />`.
 *
 * @example
 * ```tsx
 * const router = createAppRouter(queryClient, {
 *   history: createMemoryHistory({ initialEntries: ['/'] }),
 * });
 * render(
 *   <QueryClientProvider client={queryClient}>
 *     <RouterProvider router={router} />
 *   </QueryClientProvider>,
 * );
 * ```
 */
export function createAppRouter(
  queryClient: QueryClient,
  options: CreateAppRouterOptions = {},
): AppRouter {
  return buildRouter(queryClient, options.history);
}

/**
 * Registers the app's router with TanStack Router, so features type `Link to="/actions"
 * search={{ run }}` and `useNavigate()` against this tree without importing this module, and
 * types the app's own keys in the history state that `redirect`, `Link` and `navigate` write.
 */
declare module '@tanstack/react-router' {
  interface Register {
    router: AppRouter;
  }

  interface HistoryState {
    /**
     * `true` only on the `/login` entry the auth guard's 401 redirect writes
     * ({@link authenticatedRoute}); absent everywhere else. The guard has just read
     * `GET /auth/me` and the API answered 401, so that entry's first load takes the answer as
     * its session check ({@link isGuardSignedOutArrival}).
     */
    authGuardSignedOut?: true;
    /**
     * Why an open screen sent the visitor to sign in, only on the `/login` entry that
     * `src/main.tsx` opens (or replaces) for a 401 of that screen's request; absent everywhere
     * else, the guard's own 401 redirect included. `'session_ended'` after a read,
     * `'action_not_saved'` after a refused write. `LoginPage` states it as its session-ended
     * notice ({@link LoginRouteComponent}). It is history state, never search, so no link can
     * set it.
     */
    sessionEnded?: SessionEndedNotice;
  }
}
