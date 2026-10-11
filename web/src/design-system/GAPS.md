# Blitzy OS gap inventory

This is the inventory of everything in `web/src/design-system/` that is not yet bound to the Blitzy OS design system, with the procedure for binding it. Update it in the same change that closes a gap.

This document names no Blitzy OS component, prop or token value. Where the Blitzy OS side is not known it reads "Unknown".

## Status

| Item | State |
| --- | --- |
| Library | Blitzy OS Design System |
| Version | Unknown, not published |
| Status | Source unavailable |
| Package | None found on npm or other public registries |
| Source | The design frames named in the product brief were not provided. Public sources describe Blitzy OS only as Blitzy's proprietary component library and publish no components, props or token values. |

Decision: while the source is unavailable, the UI is built through this indirection layer instead of waiting for it.

- Feature code (`src/features/**`) and the app shell import UI building blocks only from `index.ts`. `web/eslint.config.js` enforces this for production feature source (`src/features/**/*.{ts,tsx}`) alone: it rejects a static import, re-export or `import()` that reaches past `index.ts` into the design system, and an `import()` by computed specifier or `import.meta.glob`, which it cannot check. Feature unit tests (`*.test.{ts,tsx}`) are exempt. Outside `src/features/**`, the app shell included, the index-only rule is a convention no lint rule checks.
- Each wrapper exposes a product-level props interface. It renders semantic HTML styled by the semantic tokens in `tokens.css`, except for these literals: structural layout values (flex factors, the `999` width-switch multipliers in `PageShell.css` and `SideNav.css`, the `0px` clamp floor in `SideNav.css`, `fr` grid tracks, the grid row spans and lines in `DateRangePicker.css`, the card header's grid line numbers in `Card.css`, Grid's column-count fallback of `1`, the `1 / 1` grid placement that stacks a tab label over its sizer in `Tabs.css` and the page frame's `100vh` minimum height), the placeholder-text `opacity: 1` reset, Grid's `min(100%, …)` track cap and the visually hidden utility's `1px` box and `inset(50%)` clip (see Token gaps), and the motion literals in `Button.css` and `Spinner.css` and the `48rem` breakpoint (see Other gaps).
- Binding Blitzy OS later changes only wrapper internals and token values. Feature code and the names exported by `index.ts` stay as they are.

## Component gaps

19 wrappers, all gaps. Each wrapper is `components/<Name>.tsx` with its stylesheet beside it. `TextField.css` also holds the field styles `Select` shares, and `components/shared.css` holds the accessibility utilities (see Other gaps).

| # | UI element | Product wrapper (file) | Product props (summary) | Blitzy OS component | Status | Proposed resolution |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Actions (Record decision, Mark done, Dismiss, Snooze, Sync now) | `Button` (`components/Button.tsx`) | `variant` (`primary`, `secondary`, `quiet`), `size` (`sm`, `md`), `disabled`, `loading`, `type`; other native button attributes; no `className`, `style` or `dangerouslySetInnerHTML` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 2 | Recommendation card | `Card` (`components/Card.tsx`) | Slots `Card.Header` (`title`, `headingLevel` 3 or 4, `rank`, `figure` (`label`, `value`: the headline number, outside the heading), `meta`), `Card.Body`, `Card.Footer` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 3 | Status and flag chips (Live, Simulated, Stale, Short window, Floor) | `Badge` (`components/Badge.tsx`) | `tone` (`neutral`, `info`, `warning`, `critical`, `success`), `children` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 4 | Global banners (discrepancies, simulated sources, stale data, short window) | `Banner` (`components/Banner.tsx`) | `tone`, `title`, `children`, `actions`, `alert` (`role="alert"`, used only for discrepancies) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 5 | Evidence and white-paper tables | `DataTable` (`components/DataTable.tsx`) | Typed `columns` (`key`, `header`, `align: 'end'` for numbers, `render`, `isRowHeader`, `width` on a scale of 4 to 16 `--space-4` steps, which lays the table out at fixed column widths when every column of a table that does not expand has one), `rows`, `getRowKey`, required `caption`, `captionLabel` (required with a rich caption), `emptyMessage`, `renderExpanded`, `expandLabel` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 6 | Section and primary navigation | `SideNav` (`components/SideNav.tsx`) | `label`, `items` (`key`, `label`, `href`, `count`, `current`), `renderLink` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 7 | Window picker | `DateRangePicker` (`components/DateRangePicker.tsx`) | `label`, `presets` (from `window.presets`), `defaultPreset`, `value`, `onSelectPreset`, `onSelectCustom` (custom range), `maxDate`, `maxDays`, `disabled`, `actions` (the caller's controls on the preset row, after the preset group), `statusMessage` (announced by a polite `role="status"` region) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 8 | Form control: text (login, decision form) | `TextField` (`components/TextField.tsx`) | `label`, `value`, `onChange`, `hint`, `error`, `required`, `disabled`, `aria-describedby` (extra description ids, such as a wrapping `Tooltip`'s, appended after the hint and error ids), `type` (`text`, `email`, `password`, `number`, `date`), or `multiline` with `rows` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 9 | Form control: single choice (decision form) | `Select` (`components/Select.tsx`) | `label`, `options` (`value`, `label`, `disabled`), `value`, `onChange`, `hint`, `error`, `required`, `disabled`, `aria-describedby` (extra description ids, such as a wrapping `Tooltip`'s, appended after the hint and error ids) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 10 | Form control: modal dialog (decision form) | `Dialog` (`components/Dialog.tsx`) | `open`, `title`, `onClose`, `children`, `footer`, `closeLabel` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 11 | Help text on metrics | `Tooltip` (`components/Tooltip.tsx`) | `content`, `children` (the trigger) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 12 | Page shell | `PageShell` (`components/PageShell.tsx`) | `topBar`, `sideNav`, `children`; heading slots `PageShell.Title` (`children`, `actions`) and `PageShell.Section` (`id`, `title`, `headingLevel` 2 or 3, `actions`); skip link to main content | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 13 | Top bar | `TopBar` (`components/TopBar.tsx`) | `productName`, `children` (actions) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 14 | Layout primitive: vertical gap | `Stack` (`components/Stack.tsx`) | `gap` (spacing step 1 to 8), `align`, `breakLongWords`, `as` (`div`, `ul`, `ol`, `section`) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 15 | Layout primitive: horizontal gap, wrap | `Inline` (`components/Inline.tsx`) | `gap`, `wrap`, `align`, `justify`, `breakLongWords`, `as` (`div`, `ul`, `ol`) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 16 | Layout primitive: columns | `Grid` (`components/Grid.tsx`) | `columns` (a count, or `{ min: 'narrow' }` or `{ min: 'wide' }`), `gap`, `as` (`div`, `ul`, `ol`); one column below the breakpoint | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 17 | Loading state | `Spinner` (`components/Spinner.tsx`) | `label`, `showLabel` (the label shown beside the ring instead of visually hidden) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 18 | Empty state | `EmptyState` (`components/EmptyState.tsx`) | `title`, `children` (the reason), `action` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 19 | Sub-list switch (Block / Expand / Review) | `Tabs` (`components/Tabs.tsx`) | `label`, `tabs` (`key`, `label`, `count`, `content`, `id`), `selectedKey`, `onChange` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |

## Token gaps

Four categories, 40 tokens, all gaps. They are declared as CSS custom properties on `:root` in `tokens.css`, and every value carries the comment `GAP: placeholder, not Blitzy OS`. The monospace family, opacity and size tokens extend the AAP 0.4.3 token mapping. `--font-family-mono` replaces the user-agent `monospace` family on code identifiers (threshold keys, property names, URNs) through the base layer's `<code>`, `<kbd>`, `<pre>` and `<samp>` rule. `--opacity-disabled` dims disabled controls in `Button.css` and `TextField.css`, `--opacity-backdrop` dims the modal backdrop in `Dialog.css`, `--size-full` carries the full-extent sizes in `DataTable.css`, `Dialog.css`, `PageShell.css` (the side navigation's full-line basis below the breakpoint and the line width in the side-navigation and main caps), `SideNav.css` (the list width and full-line ceiling in the item's width switch) and `TextField.css`, the hover-bridge offsets in `Tooltip.css` and the cap on the custom-range column minimum in `DateRangePicker.css`, and `--size-container-full` (the full inline size of the nearest inline-size query container) caps an expanded `DataTable` row's detail content at its region's visible width in `DataTable.css`. The opacity and percentage literals left in component CSS are not design values: `opacity: 1` in `TextField.css` resets the dimming some engines apply to placeholder text, so the muted placeholder keeps its contrast; `min(100%, …)` in `Grid.css` caps the auto-fit track minimum at the container width, so a single track never overflows a container narrower than that minimum; and `inset(50%)` in `shared.css` clips the visually hidden utility's `1px` box to nothing while keeping its text in the accessibility tree.

| Category | Semantic tokens | Placeholder value policy | Resolution |
| --- | --- | --- | --- |
| Colour | `--color-bg`, `--color-surface`, `--color-border`, `--color-text`, `--color-text-muted`, `--color-accent`, `--color-info`, `--color-warning`, `--color-critical`, `--color-success` | Neutral placeholders that meet WCAG 2.2 AA contrast | Replace with Blitzy OS values in `tokens.css` |
| Spacing | `--space-1`, `--space-2`, `--space-3`, `--space-4`, `--space-5`, `--space-6`, `--space-7`, `--space-8` | 4 px steps at a 16 px root, written in rem | Replace with Blitzy OS values in `tokens.css` |
| Typography | `--font-family`, `--font-family-mono`, `--font-size-xs`, `--font-size-sm`, `--font-size-md`, `--font-size-lg`, `--font-size-xl`, `--font-weight-regular`, `--font-weight-semibold`, `--line-height-tight`, `--line-height-normal` | System font stacks, a sans-serif text family and a monospace family for code, because the Content-Security-Policy allows no external font source; placeholder size, weight and line-height scale | Replace with Blitzy OS values in `tokens.css` |
| Radius, shadow, z-index, opacity, size | `--radius-sm`, `--radius-md`, `--shadow-1`, `--shadow-2`, `--z-banner`, `--z-dialog`, `--z-tooltip`, `--opacity-disabled`, `--opacity-backdrop`, `--size-full`, `--size-container-full` | Placeholders; tooltips stack above dialogs, because a tooltip can open inside a dialog; the opacities are unitless element opacities (disabled controls, the modal backdrop), not colours; `--size-full` is the full extent of the containing block, and `--size-container-full` the full inline size of the nearest inline-size query container | Replace with Blitzy OS values in `tokens.css` |

Binding the tokens:

1. The names are the stable contract. `TOKEN_NAMES` in `tokens.ts` mirrors them one-to-one in `tokens.css` order, and `tokens.test.ts` holds both files to that list. Never rename a token in feature code or in the wrappers.
2. Keep each name and replace its value. If Blitzy OS exposes its own custom properties under other names, map them inside `tokens.css`: keep the product token's declaration and set its value to the `var()` function with the Blitzy OS custom property's own name, which starts with `--`, as its only argument. Add no fallback value, because a fallback would be a second, hardcoded design value.
3. If Blitzy OS has no counterpart for a token, keep its placeholder and marker, and keep the token listed here.
4. After replacing colour values, re-check WCAG 2.2 AA contrast on both `--color-surface` and `--color-bg`: at least 4.5:1 for every text and tone colour and for surface text on `--color-accent`, and at least 3:1 for `--color-border` and the `--color-accent` focus ring.

## Other gaps

| Gap | Current state | Proposed resolution |
| --- | --- | --- |
| Layout primitives | `Stack`, `Inline` and `Grid` are product primitives. Blitzy OS primitives: Unknown | Map them to Blitzy OS primitives, keeping their props |
| Iconography | Text labels only; no icon set is shipped | Add icons only from the Blitzy OS icon source, once it is available |
| Package name, version and installation | Unknown; no Blitzy OS dependency is added while the source is unavailable | Add the package on arrival (binding procedure, step 2) |
| Grid breakpoint | CSS custom properties cannot be used in `@media` conditions, so no token can carry it. The literal `@media (max-width: 48rem)` is written out in `components/Grid.css` (single-column collapse) and `components/PageShell.css` (the side navigation stacks above main). These two stylesheets hold the only literal breakpoint outside `tokens.css`. `web/.stylelintrc.json` allows a width condition only in these two files and only at `48rem`, and `web/src/stylelint-config.test.ts` asserts that pin | Replace it with the Blitzy OS breakpoint in both stylesheets, and change the stylelint pin and its test in the same change |
| Motion | No motion tokens exist. `components/Button.css` uses a literal `150ms ease-out` shadow transition, and `components/Spinner.css` a literal `1s linear` rotation | Add motion tokens once Blitzy OS supplies them, and replace both literals |
| Line widths | No border-width token exists. Hairlines are `calc(var(--space-1) / 4)`, and focus rings and the hovered or pressed link underline `calc(var(--space-1) / 2)`, which is 1 px and 2 px at a 16 px root | Rebind to Blitzy OS border and focus values, or keep the derivation |
| Spinner shape | Component CSS allows only tokens for radius, so the ring uses `--radius-md`, which is a circle only with the placeholder values | Replace with the Blitzy OS spinner |
| Base layer | The global rules at the end of `tokens.css` are placeholders too: box sizing, body margin, body typography and colours, the bold weight, the code font family, link colour and its hover and active states, the `:focus-visible` ring and the reduced-motion override. No wrapper is a text primitive, so feature code marks bold inline text with `<b>` and `<strong>` and identifiers with `<code>`. The base layer sets `<b>` and `<strong>` to `--font-weight-semibold` in place of the user-agent `bolder`, which computes 700, a weight no token holds, and `<code>`, `<kbd>`, `<pre>` and `<samp>` to `--font-family-mono` in place of the user-agent `monospace` | Rebind to Blitzy OS base styles, keeping a visible focus ring and the reduced-motion override; bind bold text and code to the Blitzy OS text styles, or to its text components if it ships them |
| Accessibility utilities | `components/shared.css` holds the visually hidden text style and the hide-until-focused skip link | Use Blitzy OS equivalents if it ships them; otherwise keep these |
| Identifier wrapping | `components/identifierBreaks.tsx` adds a `<wbr>` after each underscore of a long identifier in API text, such as `HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`, so it wraps between its words rather than at an arbitrary character. `Banner` applies it to a string title or content and `Badge` to a string label; the index exports it for API text placed elsewhere | Keep applying it in the Blitzy OS banner and chip bindings, unless Blitzy OS breaks such identifiers at underscores itself |
| Static loading screen | `web/index.html` paints a loading screen before the bundle runs, written by hand with the classes `ds-pageshell`, `ds-pageshell--boot`, `ds-pageshell__header`, `ds-pageshell__body`, `ds-pageshell__sidenav`, `ds-pageshell__main`, `ds-topbar`, `ds-topbar__product`, `ds-sidenav`, `ds-sidenav__list`, `ds-sidenav__item`, `ds-sidenav__link`, `ds-sidenav__label`, `ds-spinner`, `ds-spinner--labelled`, `ds-spinner__circle` and `ds-spinner__label`. Its "Primary" side navigation holds one link, "Sign in" to `/login`, not marked current. `components/PageShell.css` hides it once the app renders into `#root`, and `components/PageShell.test.tsx` compares it with what `PageShell`, `TopBar`, `SideNav` and `Spinner` render | Treat these class names as a stable contract, like the token names: when `PageShell`, `TopBar`, `SideNav` or `Spinner` is rebound, keep rules for them in this folder's stylesheets, so `web/index.html` does not change, and update that test's comparison |

## Graceful degradation

Each UI element is bound at the first viable option, in this order:

1. An exact Blitzy OS component.
2. A close Blitzy OS component with prop adjustments, annotated in the wrapper and in the component table.
3. A generic Blitzy OS container styled with Blitzy OS tokens.
4. The product wrapper with a gap flag.

While the source is unavailable, all 19 elements sit at step 4.

## Compliance rules

Feature code keeps these rules whatever the binding state.

| Rule | Enforced by |
| --- | --- |
| Zero hardcoded values: every CSS value in feature code is `var(--…)` with no fallback, or one of `0`, `none`, `auto`, `inherit`, `currentColor`, `transparent`. No hex or named colours, no function other than `var()`, no breakpoints | `web/.stylelintrc.json` |
| No `style` attribute outside `src/design-system/components/` | `web/eslint.config.js` |
| No raw `<button>`, `<input>`, `<select>`, `<textarea>`, table elements (`table`, `thead`, `tbody`, `tfoot`, `tr`, `th`, `td`) or headings (`h1` to `h6`) outside `src/design-system/components/`. Use `Button`, `TextField`, `Select`, `DataTable` and the `PageShell` and `Card.Header` heading slots | `web/eslint.config.js` |
| Layout only through `Stack`, `Inline` and `Grid`: feature CSS sets no display, flex, grid, gap, alignment or float properties | `web/.stylelintrc.json` |
| Imports from the design system only through `index.ts`, for static imports and `import()` alike | `web/eslint.config.js` |

The wrappers' own stylesheets follow the same token rules for colour, spacing, typography, radius, shadow and z-index, and hold no breakpoint other than the Grid breakpoint above.

## Binding procedure

1. Obtain the Blitzy OS source: package, design frames or token files.
2. Add the package to `web/package.json`, and regenerate and commit `web/package-lock.json` in the same change, so CI's `npm ci` stays in sync.
3. Replace the token values in `tokens.css`, names unchanged (see Binding the tokens). Remove the placeholder marker from each bound value and update `tokens.test.ts`, which requires that marker on every value until then. Re-run the contrast checks.
4. Rebind each wrapper's internals in `components/`, keeping its exported props interface and its export from `index.ts`. In the component table, record the Blitzy OS component, the degradation step reached and any prop adjustment.
5. Replace the Grid breakpoint, the motion literals and the line widths listed under Other gaps. Update this document, and remove each row as its gap closes.
6. In `web/`, run `npm run lint`, `npm run typecheck`, `npm test` and the Playwright suite (`npm run e2e`). Each wrapper's `components/<Name>.test.tsx` contract test (roles, accessible names, ARIA states) must still pass.

The change touches nothing outside `web/src/design-system/**`, `web/package.json` and `web/package-lock.json`. The one exception is a breakpoint other than `48rem`, which also changes its pin in `web/.stylelintrc.json` and `web/src/stylelint-config.test.ts` (step 5).
