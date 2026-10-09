/**
 * The SPA's route tree, auth guard and authenticated app layout (AAP 0.5.5 routes; AAP 0.1.1
 * "The UI requires a login"; AAP 0.5.3 every endpoint except login and healthz needs the session).
 *
 * Routes, all code-based TanStack Router routes (no plugin, no file-based routing, no codegen):
 *
 * | Path         | Screen           | Guard and search                                        |
 * |--------------|------------------|---------------------------------------------------------|
 * | `/login`     | `LoginPage`      | none; `?redirect=` is kept only as a same-origin path   |
 * | `/`          | none             | redirects to `/actions`                                 |
 * | `/actions`   | `ActionsPage`    | signed in; `?run=<id>` addresses one run                |
 * | `/decisions` | `DecisionsPage`  | signed in                                               |
 * | `/sources`   | `SourcesPage`    | signed in                                               |
 * | `/settings`  | `SettingsPage`   | signed in (read-only)                                   |
 * | any other    | "Page not found" | signed in, so it renders inside the app shell           |
 *
 * The four signed-in screens sit under one pathless layout route, `authenticated`. Its
 * `beforeLoad` reads `GET /auth/me` through the TanStack Query cache
 * (`ensureQueryData(meQueryOptions())`), so navigation between screens re-asks nothing while the
 * cached user is present. A 401 redirects to `/login?redirect=<the requested href>`; any other
 * failure (for example 503 `database_unavailable`) is rethrown and shown by {@link RootError}
 * with a Retry button, never mistaken for a signed-out session.
 *
 * The layout ({@link AppLayout}) renders the one `PageShell` of every signed-in screen: the
 * `TopBar` with the signed-in email and "Sign out", the primary `SideNav` (Actions, Decisions,
 * Sources, Settings) and the routed page in `<main>`. Each page renders its own
 * `PageShell.Title` and sections, and `LoginPage` renders its own `PageShell`, so every screen
 * has exactly one `<main>` landmark.
 *
 * Error, not-found and pending screens render through {@link RouteFrame}: inside the layout
 * they fill `<main>` beside the navigation, and outside it (the layout's own guard failed, or the
 * login screen failed) they bring a `PageShell` of their own, so they also keep exactly one
 * `<main>` and one `<h1>`.
 *
 * Focus after navigation (WCAG 2.2 SC 2.4.3) is handled once, by the always-mounted root route:
 * see {@link useFocusRecoveryAfterNavigation}.
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
  redirect,
  useNavigate,
  useRouter,
  useRouterState,
} from '@tanstack/react-router';
import type { ErrorComponentProps, RouterHistory } from '@tanstack/react-router';
import { createContext, useContext, useEffect, useId } from 'react';
import type { ReactElement, ReactNode } from 'react';

import { isApiError, meQueryOptions, useLogout } from './api/queries';
import type { ApiError } from './api/queries';
import type { Me, RunId } from './api/types';
import {
  Banner,
  Button,
  EmptyState,
  PageShell,
  SideNav,
  Spinner,
  Stack,
  TopBar,
} from './design-system';
import type { SideNavItem, SideNavLinkProps } from './design-system';
import { ActionsPage } from './features/actions/ActionsPage';
import { LoginPage } from './features/auth/LoginPage';
import { DecisionsPage } from './features/decisions/DecisionsPage';
import { SettingsPage } from './features/settings/SettingsPage';
import { SourcesPage } from './features/sources/SourcesPage';

// ---------------------------------------------------------------------------------------------
// Texts and constants
// ---------------------------------------------------------------------------------------------

/** The product name in every top bar. */
const PRODUCT_NAME = 'Ads Action Engine';

/** Where a sign-in goes when `/login` carries no usable `redirect` (AAP 0.5.5 default page). */
const DEFAULT_AFTER_LOGIN = '/actions';

/** Accessible name of the primary navigation landmark. */
const PRIMARY_NAV_LABEL = 'Primary';

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
// Shared frame of the error, not-found and pending screens
// ---------------------------------------------------------------------------------------------

/**
 * `true` inside {@link AppLayout}, whose `PageShell` already provides the top bar, the navigation
 * and `<main>`. {@link RouteFrame} reads it to decide whether to bring a `PageShell` of its own.
 */
const InsideAppLayoutContext = createContext(false);

/** Props of {@link RouteFrame}. */
interface RouteFrameProps {
  /** The screen's content: its title slot and body. */
  children: ReactNode;
}

/**
 * The frame of a screen the router renders on its own: an error, a not-found path or a pending
 * route.
 *
 * Inside the authenticated layout the content fills that layout's `<main>`, next to the primary
 * navigation. Outside it (the layout's guard itself failed or is still pending, the login screen
 * failed, or a `notFound()` was raised outside the layout) the content gets a `PageShell` with
 * the product's `TopBar`, so the screen still has exactly one `<main>` and a skip link. Either
 * way the content is one `Stack`, spaced like the pages.
 */
function RouteFrame({ children }: RouteFrameProps): ReactElement {
  const insideAppLayout = useContext(InsideAppLayoutContext);
  const content = <Stack gap={6}>{children}</Stack>;
  if (insideAppLayout) {
    return content;
  }
  return <PageShell topBar={<TopBar productName={PRODUCT_NAME} />}>{content}</PageShell>;
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
        const focused = document.activeElement;
        if (focused === null || focused === document.body) {
          document.querySelector('main')?.focus({ preventScroll: true });
        }
      }),
    [router],
  );
}

/**
 * The root route's component: the matched child route and nothing else (the signed-in frame is
 * {@link AppLayout}'s, and `/login` renders its own), plus focus recovery after navigation.
 */
function RootComponent(): ReactElement {
  useFocusRecoveryAfterNavigation();
  return <Outlet />;
}

/**
 * The error screen of every route: the root route's `errorComponent` and the router's
 * `defaultErrorComponent`.
 *
 * It shows the "Something went wrong" title, then a critical `Banner` whose title is the
 * failure's message: the {@link ApiError} message, which the API client guarantees is safe to
 * show (for example "Database unavailable" for a 503 from the auth guard), or a generic sentence
 * for anything else. A stack trace or an internal error text is never shown. The banner has no
 * `role="alert"` (AAP 0.5.5).
 *
 * "Retry" invalidates the router, which re-runs every matched route's `beforeLoad` (the auth
 * guard reads `GET /auth/me` again, because a failed query holds no data), and resets the error
 * boundary so the route renders again.
 *
 * The title carries "Something went wrong" and the banner the specific message, so each text
 * appears once on the screen and the page keeps one `<h1>` (UI1: one `<h1>` per page).
 */
function RootError({ error, reset }: ErrorComponentProps): ReactElement {
  const router = useRouter();
  const message = isApiError(error) && error.message !== '' ? error.message : GENERIC_ERROR_MESSAGE;

  const retry = () => {
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
 * The title is the page's single `<h1>` (UI1); the empty state states why the page is empty and
 * offers the way back.
 */
function NotFound(): ReactElement {
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
 * while a route's `beforeLoad` (the auth guard's first `GET /auth/me`) takes longer than the
 * router's pending threshold.
 */
function RoutePending(): ReactElement {
  return (
    <RouteFrame>
      <Spinner label={PENDING_LABEL} />
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
 * Whether the primary destination `to` is the screen at `pathname`.
 *
 * The match is exact, ignoring one trailing slash: the four screens have no sub-pages, so a path
 * below one (`/actions/nope`) is the not-found screen and marks no link as the current page. The
 * search is ignored, so `/actions?run=202`, a custom run, still marks Actions.
 *
 * @param pathname - The router's current pathname.
 * @param to - A primary destination.
 * @returns `true` when the navigation should mark `to` as the current page.
 */
function isCurrentPath(pathname: string, to: PrimaryPath): boolean {
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

/**
 * The authenticated layout: the one `PageShell` of every signed-in screen.
 *
 * - **Top bar**: the product name, the signed-in email (plain text, not a heading) and a quiet
 *   "Sign out" button. The email is the user the auth guard read from `GET /auth/me` and put in
 *   the route context. It is read from there rather than through a `useMe()` observer: sign-out
 *   clears the query cache before the screen leaves, and an observer would then re-fetch
 *   `GET /auth/me` only to receive the 401 of the session just ended.
 * - **Sign out** posts `/auth/logout`. Once it succeeds, or fails with 401 because the session
 *   had already ended, the query cache is cleared and the screen moves to `/login`. Any other
 *   failure keeps the user signed in and shows "Sign-out failed. Try again." beside the button,
 *   linked to it through `aria-describedby`.
 * - **Primary navigation**: Actions, Decisions, Sources and Settings, in that order. The current
 *   screen's link carries `aria-current="page"`, set by `SideNav` from the router's pathname.
 * - **Main**: the routed page, or "Page not found" for a path no screen matches.
 */
function AppLayout(): ReactElement {
  const email = authenticatedRoute.useRouteContext({ select: (context) => context.me.email });
  const logout = useLogout();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const signOutErrorId = useId();

  const signOut = () => {
    logout.mutate(undefined, {
      onSettled: (_data, error) => {
        if (error === null || isSessionGone(error)) {
          queryClient.clear();
          void navigate({ to: '/login' });
        }
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
            {email !== '' && <span>{email}</span>}
            {signOutFailed && <span id={signOutErrorId}>{SIGN_OUT_FAILED_MESSAGE}</span>}
            <Button
              variant="quiet"
              loading={logout.isPending}
              aria-describedby={signOutFailed ? signOutErrorId : undefined}
              onClick={signOut}
            >
              {SIGN_OUT_LABEL}
            </Button>
          </TopBar>
        }
        sideNav={<SideNav label={PRIMARY_NAV_LABEL} items={items} renderLink={renderPrimaryLink} />}
      >
        <Outlet />
      </PageShell>
    </InsideAppLayoutContext.Provider>
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
 * `/login`, outside the authenticated layout. A rejected `redirect` is removed from the URL
 * (see {@link carriesRejectedValue}).
 */
const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  validateSearch: validateLoginSearch,
  beforeLoad: ({ location, search }): void => {
    if (carriesRejectedValue(location.search, 'redirect', search.redirect)) {
      throw redirect({ to: '/login', search: {}, replace: true });
    }
  },
  component: LoginRouteComponent,
});

/** The `/login` screen with its sanitised redirect target, `/actions` by default. */
function LoginRouteComponent(): ReactElement {
  const search = loginRoute.useSearch();
  return <LoginPage redirectTo={search.redirect ?? DEFAULT_AFTER_LOGIN} />;
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
  /** The signed-in user, as `GET /auth/me` (or sign-in) returned it. */
  me: Me;
}

/**
 * The pathless layout of the signed-in screens, with the auth guard.
 *
 * `ensureQueryData` resolves from the cache when `queryKeys.me` holds the user (seeded by
 * sign-in, or read once by an earlier navigation) and otherwise calls `GET /auth/me`, which never
 * retries. The user is returned as route context (`me`), which the layout's top bar shows. A 401
 * becomes a redirect to `/login` carrying the requested href (path, search and hash) as
 * `redirect`, so sign-in returns there. Every other failure is rethrown, so the error screen
 * shows it with Retry instead of sending a signed-in user to the login form.
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
        throw redirect({ to: '/login', search: { redirect: location.href } });
      }
      throw error;
    }
  },
  component: AppLayout,
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

/** `/decisions`: discrepancy resolution and the decision log. */
const decisionsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/decisions',
  component: DecisionsPage,
});

/** `/sources`: per-source mode, health and freshness, and "Sync now". */
const sourcesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/sources',
  component: SourcesPage,
});

/** `/settings`: the read-only configuration view. */
const settingsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/settings',
  component: SettingsPage,
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
 * renders. Links preload on intent; the only preloaded work is the auth guard, which reads the
 * cached user.
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
 * search={{ run }}` and `useNavigate()` against this tree without importing this module.
 */
declare module '@tanstack/react-router' {
  interface Register {
    router: AppRouter;
  }
}
