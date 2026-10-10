/**
 * The design-system index: the only module feature code imports UI building blocks from
 * (AAP 0.4.2).
 *
 * Every screen and component under `src/features/**`, and the app shell in `src/router.tsx`,
 * imports wrappers, their props types and the token constants from here, for example
 * `import { Button, Card, Stack, type ButtonProps } from '../../design-system';`.
 * `web/eslint.config.js` rejects any import of `design-system/components/*`,
 * `design-system/tokens` or `tokens.css` from production feature source (feature unit tests
 * are exempt), so this export list is the stable contract between feature code and the design
 * system.
 *
 * The Blitzy OS source is unavailable (AAP 0.4.1), so all 19 wrappers below are product
 * wrappers at the last graceful-degradation step (AAP 0.4.4): semantic HTML styled by the
 * semantic tokens in `tokens.css`, except for the few structural, reset, accessibility, motion
 * and breakpoint literals that `GAPS.md` lists. Binding Blitzy OS later changes only the wrapper
 * internals under `components/`, the token values in `tokens.css`, `web/package.json` and
 * `web/package-lock.json` (AAP 0.4.6), except that a breakpoint other than `48rem` also changes
 * its pin in `web/.stylelintrc.json` and `web/src/stylelint-config.test.ts`; the names exported
 * here stay as they are. Every component and token is listed as a gap in `GAPS.md`.
 *
 * The surface is explicit by design. There is no `export *`, so a helper a component module
 * keeps for itself never becomes public by accident, and no default export. This module holds
 * no logic and imports no stylesheet: each component imports its own `.css` file (and
 * `shared.css` where it uses it), so importing a component brings its styles, and `tokens.css`
 * is imported once, by `src/main.tsx`.
 *
 * Exports follow the component order of AAP 0.4.2, one group per component, with the token
 * constants last.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */

// Primary, secondary and quiet actions. GAP: bind to Blitzy OS on source arrival.
export { Button } from './components/Button';
export type { ButtonProps, ButtonSize, ButtonVariant } from './components/Button';

// Recommendation card and its slots. GAP: bind to Blitzy OS on source arrival.
export { Card } from './components/Card';
export type {
  CardBodyProps,
  CardFooterProps,
  CardHeaderProps,
  CardProps,
} from './components/Card';

// Status and flag chips. GAP: bind to Blitzy OS on source arrival.
export { Badge } from './components/Badge';
export type { BadgeProps } from './components/Badge';

// Global banners. GAP: bind to Blitzy OS on source arrival.
export { Banner } from './components/Banner';
export type { BannerProps } from './components/Banner';

// Evidence and white-paper tables. GAP: bind to Blitzy OS on source arrival.
export { DataTable } from './components/DataTable';
export type { DataTableColumn, DataTableColumnWidth, DataTableProps } from './components/DataTable';

// Section navigation. GAP: bind to Blitzy OS on source arrival.
export { SideNav } from './components/SideNav';
export type { SideNavItem, SideNavLinkProps, SideNavProps } from './components/SideNav';

// Window picker. GAP: bind to Blitzy OS on source arrival.
export { DateRangePicker } from './components/DateRangePicker';
export type { DateRangePickerProps, DateRangeValue } from './components/DateRangePicker';

// Text form control (login, decision form). GAP: bind to Blitzy OS on source arrival.
export { TextField } from './components/TextField';
// TextFieldInputProps and TextFieldMultilineProps are the two branches of the TextFieldProps
// union that TextField.tsx exports and documents as public, so they are re-exported with it.
export type {
  TextFieldInputProps,
  TextFieldMultilineProps,
  TextFieldProps,
  TextFieldType,
} from './components/TextField';

// Select form control (decision form). GAP: bind to Blitzy OS on source arrival.
export { Select } from './components/Select';
export type { SelectOption, SelectProps } from './components/Select';

// Modal dialog (decision form). GAP: bind to Blitzy OS on source arrival.
export { Dialog } from './components/Dialog';
export type { DialogProps } from './components/Dialog';

// Help text on metrics. GAP: bind to Blitzy OS on source arrival.
export { Tooltip } from './components/Tooltip';
export type { TooltipProps } from './components/Tooltip';

// Page shell and its heading slots. GAP: bind to Blitzy OS on source arrival.
export { PageShell } from './components/PageShell';
export type {
  PageShellProps,
  PageShellSectionProps,
  PageShellTitleProps,
} from './components/PageShell';

// Top bar. GAP: bind to Blitzy OS on source arrival.
export { TopBar } from './components/TopBar';
export type { TopBarProps } from './components/TopBar';

// Layout primitive: vertical stack with a token gap. GAP: bind to Blitzy OS on source arrival.
export { Stack } from './components/Stack';
export type { StackProps } from './components/Stack';

// Layout primitive: wrapping horizontal row. GAP: bind to Blitzy OS on source arrival.
export { Inline } from './components/Inline';
export type { InlineProps } from './components/Inline';

// Layout primitive: column grid. GAP: bind to Blitzy OS on source arrival.
export { Grid } from './components/Grid';
export type { GridProps } from './components/Grid';

// Loading state. GAP: bind to Blitzy OS on source arrival.
export { Spinner } from './components/Spinner';
export type { SpinnerProps } from './components/Spinner';

// Empty state. GAP: bind to Blitzy OS on source arrival.
export { EmptyState } from './components/EmptyState';
export type { EmptyStateProps } from './components/EmptyState';

// Sub-list switch (Block / Expand / Review). GAP: bind to Blitzy OS on source arrival.
export { Tabs } from './components/Tabs';
export type { TabItem, TabsProps } from './components/Tabs';

// Semantic token names, tones and spacing steps. GAP: bind to Blitzy OS on source arrival.
// cssVar is not re-exported: tokens.ts reserves it for the wrappers, and feature code styles
// through component props and var(--token) in CSS, never through computed values.
export { SPACE_STEPS, TOKEN_NAMES, TONES, tokens } from './tokens';
export type { SpaceStep, TokenName, Tone } from './tokens';
