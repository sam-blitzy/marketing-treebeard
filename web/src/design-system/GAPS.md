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

Decision: the UI is built now, through this indirection layer, instead of waiting for the source.

- Feature code (`src/features/**`) and the app shell import UI building blocks only from `index.ts`. `web/eslint.config.js` rejects every other import into the design system.
- Each wrapper exposes a product-level props interface. It renders semantic HTML styled solely by the semantic tokens in `tokens.css`.
- Binding Blitzy OS later changes only wrapper internals and token values. Feature code and the names exported by `index.ts` stay as they are.

## Component gaps

19 wrappers, all gaps. Each wrapper is `components/<Name>.tsx` with its stylesheet beside it. `TextField.css` also holds the field styles `Select` shares, and `components/shared.css` holds the accessibility utilities (see Other gaps).

| # | UI element | Product wrapper (file) | Product props (summary) | Blitzy OS component | Status | Proposed resolution |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Actions (Record decision, Mark done, Dismiss, Snooze, Sync now) | `Button` (`components/Button.tsx`) | `variant` (`primary`, `secondary`, `quiet`), `size` (`sm`, `md`), `disabled`, `loading`, `type`; other native button attributes; no `className` or `style` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 2 | Recommendation card | `Card` (`components/Card.tsx`) | Slots `Card.Header` (`title`, `headingLevel` 3 or 4, `rank`, `meta`), `Card.Body`, `Card.Footer` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 3 | Status and flag chips (Live, Simulated, Stale, Short window, Floor) | `Badge` (`components/Badge.tsx`) | `tone` (`neutral`, `info`, `warning`, `critical`, `success`), `children` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 4 | Global banners (discrepancies, simulated sources, stale data, short window) | `Banner` (`components/Banner.tsx`) | `tone`, `title`, `children`, `actions`, `alert` (`role="alert"`, used only for discrepancies) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 5 | Evidence and white-paper tables | `DataTable` (`components/DataTable.tsx`) | Typed `columns` (`key`, `header`, `align: 'end'` for numbers, `render`, `isRowHeader`), `rows`, `getRowKey`, required `caption`, `emptyMessage`, `renderExpanded`, `expandLabel` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 6 | Section and primary navigation | `SideNav` (`components/SideNav.tsx`) | `label`, `items` (`key`, `label`, `href`, `count`, `current`), `renderLink` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 7 | Window picker | `DateRangePicker` (`components/DateRangePicker.tsx`) | `label`, `presets` (from `window.presets`), `defaultPreset`, `value`, `onSelectPreset`, `onSelectCustom` (custom range), `maxDate`, `maxDays`, `disabled` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 8 | Form control: text (login, decision form) | `TextField` (`components/TextField.tsx`) | `label`, `value`, `onChange`, `hint`, `error`, `required`, `disabled`, `type` (`text`, `email`, `password`, `number`, `date`), or `multiline` with `rows` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 9 | Form control: single choice (decision form) | `Select` (`components/Select.tsx`) | `label`, `options` (`value`, `label`, `disabled`), `value`, `onChange`, `hint`, `error`, `required`, `disabled` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 10 | Form control: modal dialog (decision form) | `Dialog` (`components/Dialog.tsx`) | `open`, `title`, `onClose`, `children`, `footer`, `closeLabel` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 11 | Help text on metrics | `Tooltip` (`components/Tooltip.tsx`) | `content`, `children` (the trigger) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 12 | Page shell | `PageShell` (`components/PageShell.tsx`) | `topBar`, `sideNav`, `children`; heading slots `PageShell.Title` (`children`, `actions`) and `PageShell.Section` (`id`, `title`, `headingLevel` 2 or 3, `actions`); skip link to main content | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 13 | Top bar | `TopBar` (`components/TopBar.tsx`) | `productName`, `children` (actions) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 14 | Layout primitive: vertical gap | `Stack` (`components/Stack.tsx`) | `gap` (spacing step 1 to 8), `align`, `as` (`div`, `ul`, `ol`, `section`) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 15 | Layout primitive: horizontal gap, wrap | `Inline` (`components/Inline.tsx`) | `gap`, `wrap`, `align`, `justify`, `as` (`div`, `ul`, `ol`) | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 16 | Layout primitive: columns | `Grid` (`components/Grid.tsx`) | `columns` (a count, or `{ min: 'narrow' }` or `{ min: 'wide' }`), `gap`, `as` (`div`, `ul`, `ol`); one column below the breakpoint | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 17 | Loading state | `Spinner` (`components/Spinner.tsx`) | `label` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 18 | Empty state | `EmptyState` (`components/EmptyState.tsx`) | `title`, `children` (the reason), `action` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |
| 19 | Sub-list switch (Block / Expand / Review) | `Tabs` (`components/Tabs.tsx`) | `label`, `tabs` (`key`, `label`, `count`, `content`), `selectedKey`, `onChange` | Unknown | GAP: bind on source arrival | Wrapper renders semantic HTML styled by tokens; rebind internals to the Blitzy OS component when the source is supplied |

## Token gaps

Four categories, 35 tokens, all gaps. They are declared as CSS custom properties on `:root` in `tokens.css`, and every value carries the comment `GAP: placeholder, not Blitzy OS`.

| Category | Semantic tokens | Placeholder value policy | Resolution |
| --- | --- | --- | --- |
| Colour | `--color-bg`, `--color-surface`, `--color-border`, `--color-text`, `--color-text-muted`, `--color-accent`, `--color-info`, `--color-warning`, `--color-critical`, `--color-success` | Neutral placeholders that meet WCAG 2.2 AA contrast | Replace with Blitzy OS values in `tokens.css` |
| Spacing | `--space-1`, `--space-2`, `--space-3`, `--space-4`, `--space-5`, `--space-6`, `--space-7`, `--space-8` | 4 px steps at a 16 px root, written in rem | Replace with Blitzy OS values in `tokens.css` |
| Typography | `--font-family`, `--font-size-xs`, `--font-size-sm`, `--font-size-md`, `--font-size-lg`, `--font-size-xl`, `--font-weight-regular`, `--font-weight-semibold`, `--line-height-tight`, `--line-height-normal` | System font stack, because the Content-Security-Policy allows no external font source; placeholder size, weight and line-height scale | Replace with Blitzy OS values in `tokens.css` |
| Radius, shadow, z-index | `--radius-sm`, `--radius-md`, `--shadow-1`, `--shadow-2`, `--z-banner`, `--z-dialog`, `--z-tooltip` | Placeholders; tooltips stack above dialogs, because a tooltip can open inside a dialog | Replace with Blitzy OS values in `tokens.css` |

Binding the tokens:

1. The names are the stable contract. `TOKEN_NAMES` in `tokens.ts` mirrors them one-to-one in `tokens.css` order, and `tokens.test.ts` holds both files to that list. Never rename a token in feature code or in the wrappers.
2. Keep each name and replace its value. If Blitzy OS exposes its own custom properties under other names, map them inside `tokens.css`: the product token's value becomes `var()` of the Blitzy OS property, for example `--color-text: var(<Blitzy OS property>);`.
3. If Blitzy OS has no counterpart for a token, keep its placeholder and marker, and keep the token listed here.
4. After replacing colour values, re-check WCAG 2.2 AA contrast on both `--color-surface` and `--color-bg`: at least 4.5:1 for every text and tone colour and for surface text on `--color-accent`, and at least 3:1 for `--color-border` and the `--color-accent` focus ring.

## Other gaps

| Gap | Current state | Proposed resolution |
| --- | --- | --- |
| Layout primitives | `Stack`, `Inline` and `Grid` are product primitives. Blitzy OS primitives: Unknown | Map them to Blitzy OS primitives, keeping their props |
| Iconography | Text labels only; no icon set is shipped | Add icons only from the Blitzy OS icon source, once it is available |
| Package name, version and installation | Unknown; no dependency is added now | Add the package on arrival (binding procedure, step 2) |
| Grid breakpoint | CSS custom properties cannot be used in `@media` conditions, so no token can carry it. The literal `@media (max-width: 48rem)` is written out in `components/Grid.css` (single-column collapse) and `components/PageShell.css` (the side navigation stacks above main). These two stylesheets hold the only literal breakpoint outside `tokens.css`. `web/.stylelintrc.json` allows a width condition only in these two files and only at `48rem`, and `web/src/stylelint-config.test.ts` asserts that pin | Replace it with the Blitzy OS breakpoint in both stylesheets, and change the stylelint pin and its test in the same change |
| Motion | No motion tokens exist. `components/Button.css` uses a literal `150ms ease-out` shadow transition, and `components/Spinner.css` a literal `1s linear` rotation | Add motion tokens once Blitzy OS supplies them, and replace both literals |
| Line widths | No border-width token exists. Hairlines are `calc(var(--space-1) / 4)` and focus rings `calc(var(--space-1) / 2)`, which is 1 px and 2 px at a 16 px root | Rebind to Blitzy OS border and focus values, or keep the derivation |
| Spinner shape | Component CSS allows only tokens for radius, so the ring uses `--radius-md`, which is a circle only with the placeholder values | Replace with the Blitzy OS spinner |
| Base layer | The global rules at the end of `tokens.css` are placeholders too: box sizing, body margin, body typography and colours, link colour, the `:focus-visible` ring and the reduced-motion override | Rebind to Blitzy OS base styles, keeping a visible focus ring and the reduced-motion override |
| Accessibility utilities | `components/shared.css` holds the visually hidden text style and the hide-until-focused skip link | Use Blitzy OS equivalents if it ships them; otherwise keep these |

## Graceful degradation

Each UI element is bound at the first viable option, in this order:

1. An exact Blitzy OS component.
2. A close Blitzy OS component with prop adjustments, annotated in the wrapper and in the component table.
3. A generic Blitzy OS container styled with Blitzy OS tokens.
4. The product wrapper with a gap flag.

Today all 19 elements sit at step 4.

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
