/**
 * `BudgetSection`: the "Budget changes" section of `/actions` (AAP 0.5.5 Budget, AAP 0.5.4.5
 * B1–B4, FR-6).
 *
 * The section renders inside the shared {@link SectionFrame}: findings first, the API's groups
 * with their cards in API rank order (window spend, descending), held cards behind the held
 * disclosure, then the "Patterns withheld for sample size" list (AAP 0.5.5: "items, then
 * 'Patterns withheld for sample size'") and the suppression counts, such as the increases the
 * G1 guard blocked. Withheld B-rule shortfalls ("spending days 3 of 5 required") reach the user
 * through that frame list only and are never rendered a second time here.
 *
 * Each card is a {@link RecommendationCard} at heading level 3, directly under the section's
 * `<h2>`: the section's only group, "Budget changes", repeats the section's title, so the frame
 * gives it no `<h3>` ({@link groupRepeatsSectionTitle}). A card in a group with a title of its
 * own sits at level 4, below that group's `<h3>`, so no heading level is skipped. The card
 * already shows the reason, the evidence table (which carries B4's weekend and weekday
 * counts and its p-value), the window chip, the flags (including `shared_budget`) and the
 * "Record decision" footer for B1, B2 and B3. This section adds the budget field set of
 * AAP 0.5.5 as the card's children, in this order:
 *
 * 1. **Utilization beside efficiency** (AAP 0.5.4.5 "side by side"): a two-column `Grid` whose
 *    "Utilization" cell holds the window utilization ("n/a" for a lifetime or custom-period
 *    budget, as the API marks it, AAP 0.5.4.1) and a one-line summary of the spending days and
 *    the rule's predicate days (B4: weekend days, then weekdays, with spend) against what the
 *    evidence requires, and whose "Efficiency" cell holds cost per outcome against the account
 *    baseline. The grid stacks to one column below its breakpoint (design system).
 * 2. **Budget days**: a captioned table of every day count against its requirement, in the
 *    order of AAP 0.5.4.1 Evidence whatever the API's metric order: known-budget (or
 *    budget-active) days, spending days, the predicate days (capped, high-utilization or
 *    under-utilized days; B4's weekend days, then weekdays, with spend), known idle,
 *    changed-intraday and unobserved days. Each requirement is the API's: the rule's gate, or
 *    for a B4 card's known-budget and spending days the budget-history minimums it carries
 *    beside the pattern. A count nothing requires (known idle, changed-intraday, unobserved)
 *    shows "—". Shares are the backend's metrics and are never computed here. B4's outcome
 *    minimum appears, observed against required, as the evidence table's sample line.
 * 3. **Budget details**: outcomes, CRM Qualified Leads (only when linked), the latest effective
 *    daily budget and the proposed daily budget.
 * 4. **Shared budget members**: every member campaign with its member days (AAP 0.5.4.5 dated
 *    membership), on a shared-budget subject.
 * 5. **B1 funding**: how much of the increase B3 reallocations fund and how much comes from new
 *    budget (AAP 0.5.4.2 "Presenting reallocations").
 * 6. **B3 allocations**: each allocation as funding for a named B1, linked to that B1's card when
 *    the evidence names it, then the plain decrease that funds no B1.
 *
 * Presentation rules this file keeps:
 * - Money stays a decimal string in its account currency: each amount is formatted on its own,
 *   never summed, subtracted or converted in the browser (AAP 0.1.1). Totals such as a B1's
 *   funded share are the backend's.
 * - Every label, threshold and day minimum comes from the API; no threshold or window length is
 *   written into the client (AAP 0.5.4.1). Absent values render "unavailable", never `0`.
 * - Built only on the design-system index (AAP 0.4.4): no raw tables, buttons or headings, no
 *   inline styles and no stylesheet. Layout comes from `Grid` and `Stack` alone.
 *
 * @packageDocumentation
 */
import type { ReactElement, ReactNode } from 'react';

import type { Item, Metric } from '../../../api/types';
import { DataTable, Grid, Stack } from '../../../design-system';
import type { DataTableColumn } from '../../../design-system';
import {
  EM_DASH,
  NOT_APPLICABLE,
  UNAVAILABLE,
  formatCount,
  formatMetric,
  formatMoney,
  isDecimalString,
} from '../../../lib/format';
import { RecommendationCard } from '../RecommendationCard';
import {
  allocations,
  baselineByKey,
  dayCounts,
  evidenceCurrency,
  freedAmount,
  funding,
  metricByKey,
  proposedDailyBudget,
  sharedMembers,
  unallocatedAmount,
} from '../evidenceExtra';
import type { Allocation, DayCount, Funding, SharedMember } from '../evidenceExtra';
import { FieldList, SectionFrame, groupRepeatsSectionTitle } from './SectionFrame';
import type { FieldItem, SectionComponentProps } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Labels and backend keys
// ---------------------------------------------------------------------------------------------

/** The title of the section's withheld list (AAP 0.5.5 Budget). */
const WITHHELD_TITLE = 'Patterns withheld for sample size';

/** Separator between the parts of one line: a middle dot (U+00B7) with spaces. */
const SEPARATOR = ' · ';

/**
 * The backend metric and baseline keys this section reads (`analysis.budget.budget_evidence`,
 * `_history_metrics` and the B4 card). They are machine keys, not thresholds.
 */
const KEYS = Object.freeze({
  utilization: 'window_utilization',
  costPerOutcome: 'cost_per_outcome',
  baseline: 'baseline_cost_per_outcome',
  outcomes: 'outcomes',
  ql: 'ql',
  dailyBudget: 'daily_budget',
  proposedDailyBudget: 'proposed_daily_budget',
  spendingDays: 'spending_days',
} as const);

/**
 * The day-count keys of the predicate a budget rule counts: the daily predicates of
 * `analysis.budget._PREDICATES`, capped days (C2/B1), high-utilization days (B2) and
 * under-utilized days (B3), and B4's weekend days and weekdays with spend
 * (`budget._b4_candidate`, gated on `b4.min_weekend_days` and `b4.min_weekdays`).
 */
const PREDICATE_DAY_KEYS: ReadonlySet<string> = new Set([
  'capped_days',
  'high_utilization_days',
  'under_utilized_days',
  'weekend_days',
  'weekdays',
]);

/** The rule whose card shows how its increase is funded (AAP 0.5.4.2). */
const B1 = 'B1';

/** The rule whose card shows each reallocation as funding for a named B1 (AAP 0.5.4.2). */
const B3 = 'B3';

/** The flag a shared-budget item carries (`budget._shared_flag`). */
const SHARED_BUDGET_FLAG = 'shared_budget';

// ---------------------------------------------------------------------------------------------
// Utilization and efficiency
// ---------------------------------------------------------------------------------------------

/**
 * The window utilization as display text: the ratio as a percentage, or "n/a" when the API marks
 * the measure as not applying (a lifetime or custom-period budget, or no known-budget active day;
 * AAP 0.5.4.1). The API's "n/a" is passed through `formatMetric` verbatim and normalised to
 * {@link NOT_APPLICABLE}, so every not-applicable utilization reads the same.
 */
function utilizationText(metric: Metric | null): string {
  if (metric === null) {
    return UNAVAILABLE;
  }
  const text = formatMetric(metric);
  return text.trim().toLowerCase() === NOT_APPLICABLE ? NOT_APPLICABLE : text;
}

/** A day count's figure against its requirement: `"26 / 5"`, or `"20 / —"` when none applies. */
function dayRatio(count: DayCount): string {
  const required = count.required === null ? EM_DASH : formatCount(count.required);
  return `${formatCount(count.observed)} / ${required}`;
}

/**
 * The one-line summary of the utilization cell: the spending days and the rule's predicate days,
 * each observed against required, in {@link dayCounts} order, for example "Spending days 26 / 5 ·
 * Capped days 22 / 5", or for B4 "Spending days 28 / 5 · Weekend days with spend 8 / 4 ·
 * Weekdays with spend 20 / 10". `null` when the evidence has neither count.
 */
function daySummary(counts: readonly DayCount[]): string | null {
  const parts = counts
    .filter((count) => count.key === KEYS.spendingDays || PREDICATE_DAY_KEYS.has(count.key))
    .map((count) => `${count.label} ${dayRatio(count)}`);
  return parts.length === 0 ? null : parts.join(SEPARATOR);
}

/**
 * Whether a metric is a known zero: present, not marked unavailable, and numerically 0. Only
 * counts are tested this way (outcomes), never money.
 */
function isKnownZero(metric: Metric | null): boolean {
  if (metric === null || metric.unavailable === true) {
    return false;
  }
  const { value } = metric;
  if (typeof value === 'number') {
    return value === 0;
  }
  return typeof value === 'string' && isDecimalString(value) && Number(value) === 0;
}

/**
 * Cost per outcome as display text. A cost the backend computed renders through `formatMetric`.
 * Without one, a known zero outcome count renders "—" (no outcome to divide by, as the backend's
 * own reason sentence states it), and anything else renders "unavailable".
 */
function costText(cost: Metric | null, outcomes: Metric | null): string {
  if (cost !== null && cost.unavailable !== true && cost.value !== null) {
    return formatMetric(cost);
  }
  return isKnownZero(outcomes) ? EM_DASH : UNAVAILABLE;
}

/** The fields of the "Utilization" cell: window utilization, then the day summary. */
function utilizationFields(item: Item, counts: readonly DayCount[]): FieldItem[] {
  const utilization = metricByKey(item, KEYS.utilization);
  const fields: FieldItem[] = [
    {
      key: KEYS.utilization,
      label: utilization?.label ?? 'Window utilization',
      value: utilizationText(utilization),
    },
  ];
  const summary = daySummary(counts);
  if (summary !== null) {
    fields.push({ key: 'day-summary', label: 'Days (observed / required)', value: summary });
  }
  return fields;
}

/**
 * The fields of the "Efficiency" cell: cost per outcome, then the account baseline it is
 * compared against, formatted in the cost metric's unit and currency (else the evidence
 * currency), so both amounts read in the same account currency.
 */
function efficiencyFields(item: Item): FieldItem[] {
  const cost = metricByKey(item, KEYS.costPerOutcome);
  const outcomes = metricByKey(item, KEYS.outcomes);
  const baseline = baselineByKey(item, KEYS.baseline);
  const currency = cost?.currency ?? evidenceCurrency(item);
  return [
    {
      key: KEYS.costPerOutcome,
      label: cost?.label ?? 'Cost per outcome',
      value: costText(cost, outcomes),
    },
    {
      key: KEYS.baseline,
      label: baseline?.label ?? 'Account baseline cost per outcome',
      value:
        baseline === null
          ? UNAVAILABLE
          : formatMetric({ value: baseline.value, unit: cost?.unit ?? null, currency }),
    },
  ];
}

/**
 * Utilization beside efficiency (AAP 0.5.5: "utilization and efficiency in adjacent columns").
 * Two `Stack` cells in one two-column `Grid`, each named and titled, so a screen reader announces
 * "Utilization" and "Efficiency" as groups and a sighted reader sees the same titles. Rendered
 * only when the evidence carries a utilization or a cost-per-outcome metric.
 */
function UtilizationEfficiency({
  item,
  counts,
}: {
  item: Item;
  counts: readonly DayCount[];
}): ReactElement | null {
  const hasUtilization = metricByKey(item, KEYS.utilization) !== null;
  const hasCost = metricByKey(item, KEYS.costPerOutcome) !== null;
  if (!hasUtilization && !hasCost) {
    return null;
  }
  return (
    <Grid columns={2} aria-label="Utilization and efficiency">
      {/*
        BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text or label primitive, and feature
        code carries no CSS (AAP 0.4.4). Each cell's title is therefore a <span> in a Stack; the
        Stack's accessible name repeats it so the cell is announced as a named group.
      */}
      <Stack gap={1} aria-label="Utilization">
        <span>Utilization</span>
        <FieldList fields={utilizationFields(item, counts)} />
      </Stack>
      <Stack gap={1} aria-label="Efficiency">
        <span>Efficiency</span>
        <FieldList fields={efficiencyFields(item)} />
      </Stack>
    </Grid>
  );
}

// ---------------------------------------------------------------------------------------------
// Budget days
// ---------------------------------------------------------------------------------------------

/** One row of the "Budget days" table: a day count with a key unique within the table. */
interface DayRow extends DayCount {
  /** The React key and row key: the metric key plus its position, unique even if a key repeats. */
  readonly rowKey: string;
}

/** The columns of the "Budget days" table: the day type as row header, then the two counts. */
const DAY_COLUMNS: readonly DataTableColumn<DayRow>[] = Object.freeze([
  { key: 'label', header: 'Day type', isRowHeader: true, render: (row: DayRow) => row.label },
  {
    key: 'observed',
    header: 'Observed',
    align: 'end',
    render: (row: DayRow) => formatCount(row.observed),
  },
  {
    key: 'required',
    header: 'Required',
    align: 'end',
    render: (row: DayRow) => (row.required === null ? EM_DASH : formatCount(row.required)),
  },
]);

/**
 * Every day count of the card against its requirement, in {@link dayCounts} order (AAP 0.5.4.1
 * Evidence: known-budget or budget-active, spending, predicate, idle, changed-intraday and
 * unobserved days), with "—" for a count nothing requires. `null` when the evidence has no day
 * counts.
 */
function BudgetDays({ counts }: { counts: readonly DayCount[] }): ReactElement | null {
  if (counts.length === 0) {
    return null;
  }
  const rows: DayRow[] = counts.map((count, index) => ({
    ...count,
    rowKey: `${count.key}-${index}`,
  }));
  return (
    <DataTable
      caption="Budget days"
      columns={DAY_COLUMNS}
      rows={rows}
      getRowKey={(row) => row.rowKey}
    />
  );
}

// ---------------------------------------------------------------------------------------------
// Budget details
// ---------------------------------------------------------------------------------------------

/**
 * The card's labelled budget fields, in order: outcomes, CRM Qualified Leads (only when the
 * evidence carries them, which it does only for a linked CRM), the latest effective daily budget
 * and the proposed daily budget (only when the item proposes one). Labels are the backend's
 * metric labels ("Window conversions", "Qualified Leads (CRM)", "Latest effective daily
 * budget"), so the field names the same quantity the reason and the evidence table name.
 */
function detailFields(item: Item): FieldItem[] {
  const outcomes = metricByKey(item, KEYS.outcomes);
  const ql = metricByKey(item, KEYS.ql);
  const dailyBudget = metricByKey(item, KEYS.dailyBudget);
  const proposed = proposedDailyBudget(item);

  const fields: FieldItem[] = [
    {
      key: KEYS.outcomes,
      label: outcomes?.label ?? 'Outcomes',
      value: outcomes === null ? null : formatMetric(outcomes),
    },
  ];
  if (ql !== null) {
    fields.push({ key: KEYS.ql, label: ql.label, value: formatMetric(ql) });
  }
  fields.push({
    key: KEYS.dailyBudget,
    label: dailyBudget?.label ?? 'Daily budget',
    value: dailyBudget === null ? null : formatMetric(dailyBudget),
  });
  if (proposed !== null) {
    fields.push({
      key: KEYS.proposedDailyBudget,
      label: metricByKey(item, KEYS.proposedDailyBudget)?.label ?? 'Proposed daily budget',
      value: formatMoney(proposed, evidenceCurrency(item)),
    });
  }
  return fields;
}

// ---------------------------------------------------------------------------------------------
// Shared budget members
// ---------------------------------------------------------------------------------------------

/** Whether the item is about a Google explicitly shared budget (subject or flag). */
function isSharedBudget(item: Item): boolean {
  const flagged = (flags: readonly { code: string }[]): boolean =>
    flags.some((flag) => flag.code === SHARED_BUDGET_FLAG);
  return (
    item.decision_subject?.type === 'budget' ||
    flagged(item.flags) ||
    flagged(item.current.flags_now)
  );
}

/** A member's days as text: "24 member days", "1 member day", or unavailable when not given. */
function memberDaysText(memberDays: number | null): string {
  if (memberDays === null) {
    return `member days ${UNAVAILABLE}`;
  }
  return `${formatCount(memberDays)} ${memberDays === 1 ? 'member day' : 'member days'}`;
}

/**
 * Every member campaign of a shared budget with its member days (AAP 0.5.5: "Shared-budget cards
 * list every member campaign with its member days"). Shown on a shared-budget subject, and on any
 * card whose evidence lists members (a B4 card on a member campaign shows its shared budget's
 * history). A shared budget whose members the evidence does not list says so.
 */
function SharedBudgetMembers({ item }: { item: Item }): ReactElement | null {
  const members: readonly SharedMember[] = sharedMembers(item);
  if (members.length === 0 && !isSharedBudget(item)) {
    return null;
  }
  return (
    <Stack gap={1}>
      <span>Shared budget members</span>
      {members.length === 0 ? (
        <span>{`Members ${UNAVAILABLE}`}</span>
      ) : (
        <Stack as="ul" gap={1} aria-label="Shared budget members">
          {members.map((member, index) => (
            <li key={`${member.id}-${index}`}>
              {`${member.name}${SEPARATOR}${memberDaysText(member.memberDays)}`}
            </li>
          ))}
        </Stack>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// B1 funding and B3 allocations (AAP 0.5.4.2 "Presenting reallocations")
// ---------------------------------------------------------------------------------------------

/**
 * How a B1 increase is funded: the backend's sentence (`budget.render_b1_funding`) when the
 * evidence carries it, else the same sentence built from the API's own amounts, "Raise by
 * {increase}: {funded by reallocation} funded by reallocation from {donors}, {from new budget}
 * from new budget", with each donor named once in allocation order and the "from {donors}"
 * clause left out when there is none. No amount is added or subtracted here: both parts are the
 * backend's. `null` when either part is missing, because a partial split would misstate it.
 */
function fundingText(funded: Funding, currency: string | null): string | null {
  if (funded.text !== null) {
    return funded.text;
  }
  if (funded.fromReallocation === null || funded.fromNewBudget === null) {
    return null;
  }
  const donors = [...new Set(funded.donors.map((donor) => donor.name.trim()))];
  const from = donors.length > 0 ? ` from ${donors.join(', ')}` : '';
  return (
    `Raise by ${formatMoney(funded.increase, currency)}: ` +
    `${formatMoney(funded.fromReallocation, currency)} funded by reallocation${from}, ` +
    `${formatMoney(funded.fromNewBudget, currency)} from new budget`
  );
}

/** The B1 funding line, or `null` when the item is no B1 or its funding is not recorded. */
function B1Funding({ item }: { item: Item }): ReactElement | null {
  if (item.rule_id !== B1) {
    return null;
  }
  const funded = funding(item);
  const text = funded === null ? null : fundingText(funded, evidenceCurrency(item));
  return text === null ? null : <div>{text}</div>;
}

/**
 * One B3 allocation as funding for a named B1: "Move {amount} to fund {receiver}'s recommended
 * increase (B1)" (`budget.render_b3_allocation`).
 *
 * Without the receiver's item id, the backend's line is shown as given (or built from the
 * allocation's own amount when the line is missing). With it, the receiver's name becomes an
 * in-page link to that B1 card (`#item-{id}`, the card's own id): the backend's line is split
 * around the name so its wording and amount stay exactly as rendered, and the line is rebuilt
 * from the allocation only when the name does not appear in it.
 */
function AllocationLine({
  allocation,
  currency,
}: {
  allocation: Allocation;
  currency: string | null;
}): ReactNode {
  const name = allocation.receiverName.trim();
  const amount = formatMoney(allocation.amount, currency);
  if (allocation.receiverItemId === null) {
    return allocation.text ?? `Move ${amount} to fund ${name}'s recommended increase (B1)`;
  }
  const link = <a href={`#item-${allocation.receiverItemId}`}>{name}</a>;
  const text = allocation.text;
  const at = text === null ? -1 : text.indexOf(name);
  if (text === null || at < 0) {
    return (
      <>
        {`Move ${amount} to fund `}
        {link}
        {"'s recommended increase (B1)"}
      </>
    );
  }
  return (
    <>
      {text.slice(0, at)}
      {link}
      {text.slice(at + name.length)}
    </>
  );
}

/**
 * A B3 card's reallocations, one line each in allocation order, then the plain decrease: the
 * freed amount that funds no B1 (`extra.unallocated`). When the evidence records no split, a
 * card with no allocation shows its whole freed amount as the plain decrease; one with
 * allocations but no recorded remainder shows none, since the remainder is never computed here.
 * `null` for any other rule, or when there is nothing to show.
 */
function B3Allocations({ item }: { item: Item }): ReactElement | null {
  if (item.rule_id !== B3) {
    return null;
  }
  const given = allocations(item);
  const currency = evidenceCurrency(item);
  const plain = unallocatedAmount(item) ?? (given.length === 0 ? freedAmount(item) : null);
  if (given.length === 0 && plain === null) {
    return null;
  }
  return (
    <Stack gap={1}>
      {given.length === 0 ? null : (
        <Stack as="ul" gap={1} aria-label="Reallocations to recommended increases">
          {given.map((allocation, index) => (
            <li key={`${allocation.receiverType ?? 'subject'}-${allocation.receiverId}-${index}`}>
              <AllocationLine allocation={allocation} currency={currency} />
            </li>
          ))}
        </Stack>
      )}
      {plain === null ? null : <div>{`Plain decrease: ${formatMoney(plain, currency)}`}</div>}
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// The card body and the section
// ---------------------------------------------------------------------------------------------

/**
 * The budget field set of one card (AAP 0.5.5 Budget), in the order the module overview lists.
 * Each part renders only when its evidence is present, so B2 and B4 cards show what applies to
 * them (a B4 card on a lifetime budget shows utilization "n/a" and no B1 or B3 lines).
 */
function BudgetCardDetails({ item }: { item: Item }): ReactElement {
  const counts = dayCounts(item);
  return (
    <>
      <UtilizationEfficiency item={item} counts={counts} />
      <BudgetDays counts={counts} />
      <FieldList fields={detailFields(item)} label="Budget details" />
      <SharedBudgetMembers item={item} />
      <B1Funding item={item} />
      <B3Allocations item={item} />
    </>
  );
}

/**
 * The "Budget changes" section of `/actions` (AAP 0.5.5 Budget; AAP 0.5.4.5 B1–B4).
 *
 * Renders the section through {@link SectionFrame} with the withheld list titled "Patterns
 * withheld for sample size". Every item becomes a {@link RecommendationCard}, in API rank order:
 * at heading level 3 in a group whose title repeats the section's, which has no `<h3>`
 * ({@link groupRepeatsSectionTitle}), and at level 4 below any other group's `<h3>`. Its
 * children are the budget field set: utilization beside efficiency, the "Budget days" table,
 * the budget details, the shared-budget members, the B1 funding line and the B3 allocation
 * lines (see the module overview). Withheld patterns, findings, held cards and suppression
 * counts are the frame's.
 *
 * Fetches nothing and holds no state: everything shown comes from `section`, as
 * `GET /recommendations` returned it.
 *
 * @param props - see {@link SectionComponentProps}; only `section` is read.
 * @returns the section region
 *
 * @example
 * ```tsx
 * const budget = page.sections.find((section) => section.key === 'budget');
 * return budget === undefined ? null : (
 *   <BudgetSection section={budget} run={page.run} overlay={page.overlay} />
 * );
 * ```
 */
export function BudgetSection({ section }: SectionComponentProps): ReactElement {
  return (
    <SectionFrame
      section={section}
      withheldTitle={WITHHELD_TITLE}
      renderItem={(item, group) => (
        <RecommendationCard
          item={item}
          headingLevel={groupRepeatsSectionTitle(section, group) ? 3 : 4}
        >
          <BudgetCardDetails item={item} />
        </RecommendationCard>
      )}
    />
  );
}
