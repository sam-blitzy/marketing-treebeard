/**
 * Help text for the metrics a recommendation cites (AAP 0.4.2 "Help text on metrics"), shown
 * through the design system's `Tooltip` beside the metric's label.
 *
 * Each text is a short, plain definition of the backend metric key it is filed under
 * (`backend/src/ads_engine/analysis/*.py`), taken from the product's own definitions: the
 * shared definitions of window, outcome, efficiency, budget days and the CRM pipeline
 * (AAP 0.5.4.1), the keyword, campaign and creative rules (AAP 0.5.4.3, 0.5.4.4, 0.5.4.6) and
 * the white-paper attribution chain (AAP 0.5.4.7). A key whose label already says everything,
 * such as `spend` or `clicks`, or whose meaning differs between rules, such as `leads`, has no
 * entry, so its label renders without help.
 *
 * Rules every text keeps:
 * - **No configured value is written here.** Thresholds, periods and levels come from
 *   configuration and may change, so a text names "the configured minimum" rather than a
 *   number (AAP 0.5.4.1: the UI renders configured values, never literals). The evidence's own
 *   labels and "Thresholds applied" list carry the values in force for the run.
 * - **One meaning per key.** A key that several rules emit, such as `ql` or `cost_per_outcome`,
 *   has a text that is true in every one of them.
 *
 * No React and no I/O: a pure lookup.
 *
 * @example
 * ```ts
 * metricHelp('window_utilization');
 * // "Window spend divided by the sum of daily budgets, both over the known-budget active days …"
 * metricHelp('spend'); // undefined
 * ```
 *
 * @packageDocumentation
 */

/** The help text of each metric key, by key. Keys are the backend's machine keys, verbatim. */
const HELP: ReadonlyMap<string, string> = new Map<string, string>([
  // Window, outcomes and efficiency (AAP 0.5.4.1).
  [
    'window_days',
    'Complete days in the analysis window. Each source keeps its own native days, and today is ' +
      'left out as a partial day.',
  ],
  [
    'outcomes',
    'Google: conversions. LinkedIn: one-click leads on lead-generation campaigns, external ' +
      'website conversions on other objectives.',
  ],
  [
    'cost_per_outcome',
    'Efficiency: window spend divided by window outcomes. It is compared with the account ' +
      'baseline, the average for the same source and objective group over the same window.',
  ],
  [
    'expected_outcomes',
    'Window spend divided by the account baseline cost per outcome: the outcomes expected at ' +
      'the account’s average efficiency.',
  ],

  // Budget history and utilization (AAP 0.5.4.1, 0.5.4.5).
  [
    'window_utilization',
    'Window spend divided by the sum of daily budgets, both over the known-budget active days ' +
      '(budget-active days for a shared budget). Days whose budget is unknown are left out. ' +
      'Applies to daily budgets only.',
  ],
  [
    'known_days',
    'Days whose daily budget and status are known for the whole day, with the campaign ' +
      'active: observations bracket the day, agree, and are no further apart than the ' +
      'configured maximum gap. For a shared budget: days whose amount and membership are known ' +
      'and at least one member is active.',
  ],
  ['spending_days', 'Known-budget active days with spend above zero.'],
  ['spending_day_share', 'Spending days divided by known-budget active days.'],
  [
    'idle_days',
    'Known-budget active days with zero spend: an observed idle day, not an unknown one.',
  ],
  [
    'changed_intraday_days',
    'Days whose observations before and after disagree, so the budget in effect is unknown. ' +
      'They are left out of utilization and cap counts.',
  ],
  [
    'unobserved_days',
    'Days without an observation before or after them, or inside a gap longer than the ' +
      'configured maximum. Their budget is unknown, so they are left out of utilization and cap ' +
      'counts.',
  ],
  [
    'capped_days',
    'Known-budget active days whose spend reached the configured capped-day share of the daily ' +
      'budget or, on Google, whose search impression share lost to budget reached the ' +
      'configured level.',
  ],
  ['capped_days_share', 'Capped days divided by known-budget active days.'],
  [
    'high_utilization_days',
    'Known-budget active days whose spend reached the configured high-utilization share of the ' +
      'daily budget.',
  ],
  ['high_utilization_days_share', 'High-utilization days divided by known-budget active days.'],
  [
    'under_utilized_days',
    'Known-budget active days whose spend stayed at or below the configured under-utilization ' +
      'share of the daily budget, known idle days included.',
  ],
  ['under_utilized_days_share', 'Under-utilized days divided by known-budget active days.'],
  ['spend_days', 'Window days on which the campaign spent.'],
  ['spend_day_share', 'Days with spend divided by window days.'],

  // CRM pipeline (AAP 0.5.4.1).
  [
    'matured_leads',
    'Leads in the window whose touch came at least the configured maturity period before ' +
      'window end. Counted before CRM matching, so unmatched leads stay in.',
  ],
  ['matched_leads', 'Matured leads matched to a HubSpot contact.'],
  [
    'unresolved',
    'Matured leads not matched to a contact: pending, no match, no email or error. They stay ' +
      'in the count and are never dropped.',
  ],
  [
    'unresolved_share',
    'Unresolved matured leads divided by all matured leads. At or above the configured floor ' +
      'share, the pipeline is unavailable.',
  ],
  [
    'eligible_contacts',
    'Distinct contacts behind the matched matured leads. A contact with repeat leads counts ' +
      'once, at its earliest matured touch.',
  ],
  [
    'unknown_evidence_share',
    'Share of eligible contacts whose CRM evidence is unknown, because a contact, its stage ' +
      'dates or an associated meeting or deal could not be read. Any share above zero makes ' +
      'the pipeline veto unavailable.',
  ],
  [
    'mql',
    'Contacts that entered the MQL stage named in the HubSpot mapping at or after their touch. ' +
      'Contacts already in that stage before the touch are not counted.',
  ],
  [
    'ql',
    'Contacts that entered the Qualified Lead stage at or after their touch, read from that ' +
      'stage’s own HubSpot date and never derived from MQL.',
  ],
  ['meetings', 'Meetings of the eligible contacts, counted only at or after each contact’s touch.'],
  [
    'young_leads',
    'Leads whose touch falls inside the configured maturity period before window end: too ' +
      'recent to judge, so their outcomes never enter the veto.',
  ],
  [
    'young_ql',
    'Qualified Leads from those recent leads, shown for context; never part of the veto.',
  ],

  // White-paper attribution (AAP 0.5.4.7).
  [
    'linkedin_leads',
    'Lead form responses submitted in the window, as LinkedIn reports them. A platform count, ' +
      'never added to CRM counts.',
  ],
  [
    'meetings_booked',
    'Meetings created and starting at or after the touch, from a configured booking source and ' +
      'without an excluded outcome. HubSpot records no booking time, so record creation stands ' +
      'in for it.',
  ],
  [
    'other_meetings',
    'Meetings from other sources, such as imported, synced or logged ones, that pass the same ' +
      'time checks but may have been created after the real booking. Left out of cost per ' +
      'meeting.',
  ],
  ['deals_after_touch', 'Deals created at or after the contact’s touch.'],
  ['cost_per_ql', 'Spend divided by Qualified Leads, within one currency.'],
  ['cost_per_meeting', 'Spend divided by meetings booked; other meetings are not counted.'],

  // Creative stage rates and tests (AAP 0.5.4.6).
  [
    'click_rate',
    'Clicks divided by impressions, compared with the account baseline for LinkedIn creatives ' +
      'of the same objective in the window.',
  ],
  [
    'open_rate',
    'Lead form opens divided by impressions, compared with the account baseline for LinkedIn ' +
      'creatives of the same objective in the window.',
  ],
  [
    'completion_rate',
    'Leads divided by lead form opens, compared with the account baseline for LinkedIn ' +
      'creatives of the same objective in the window.',
  ],
  [
    'click_rate_ci_low',
    'Lower bound of the click rate’s Wilson interval at the configured confidence level.',
  ],
  [
    'click_rate_ci_high',
    'Upper bound of the click rate’s Wilson interval at the configured confidence level. The ' +
      'stage is below baseline when this bound is under the account baseline.',
  ],
  [
    'open_rate_ci_low',
    'Lower bound of the open rate’s Wilson interval at the configured confidence level.',
  ],
  [
    'open_rate_ci_high',
    'Upper bound of the open rate’s Wilson interval at the configured confidence level. The ' +
      'stage is below baseline when this bound is under the account baseline.',
  ],
  [
    'completion_rate_ci_low',
    'Lower bound of the completion rate’s Wilson interval at the configured confidence level.',
  ],
  [
    'completion_rate_ci_high',
    'Upper bound of the completion rate’s Wilson interval at the configured confidence level. ' +
      'The stage is below baseline when this bound is under the account baseline.',
  ],
  [
    'expected_min_cell',
    'The smallest count the variant comparison expects in any cell. When it is small, Fisher’s ' +
      'exact test replaces the two-proportion z-test.',
  ],
  [
    'additional_impressions_per_variant',
    'Roughly how many more impressions each variant needs before the observed difference could ' +
      'be distinguishable, at the configured significance level and power.',
  ],
  [
    'sample_per_variant',
    'The sample each variant needs to detect the configured target lift, at the configured ' +
      'significance level and power, counted in the tested stage’s own denominator.',
  ],
  [
    'projected_impressions_per_variant',
    'The form-open sample divided by the observed open rate: a projection, not a measurement.',
  ],
  [
    'projected_days',
    'The sample per variant divided by each variant’s current daily delivery: a projection.',
  ],

  // Keywords (AAP 0.5.4.3).
  [
    'conversions',
    'Google conversions in the window; data-driven attribution can make them fractional.',
  ],
  ['cost_per_conversion', 'Spend divided by conversions; shown as — when there are none.'],
  ['wasted_spend', 'The search term’s whole window spend, none of which produced a conversion.'],
  [
    'min_clicks',
    'Clicks a term needs before it is judged: the configured rule-of-three value divided by the ' +
      'account’s conversions per click, never below the configured floor. A heuristic, not a ' +
      'probability.',
  ],
  [
    'qs_first',
    'Google quality score at the start of the window: the mean daily score over the first ' +
      'configured span, or the window-start snapshot when daily scores are absent.',
  ],
  [
    'qs_last',
    'Google quality score at the end of the window: the mean daily score over the last ' +
      'configured span, or the window-end snapshot when daily scores are absent.',
  ],
  ['cpc_ratio', 'Second-half cost per click divided by first-half cost per click.'],
  ['cpa_ratio', 'Second-half cost per conversion divided by first-half cost per conversion.'],

  // Launch segments (AAP 0.5.4.4 C3a).
  ['segment_cpl', 'Segment spend divided by segment leads.'],
  ['campaign_cpl', 'Campaign spend divided by campaign leads.'],
  ['cpl_ratio', 'Segment cost per lead divided by the campaign’s cost per lead.'],
  ['impression_share', 'Segment impressions divided by the campaign’s impressions.'],

  // Discrepancies (AAP 0.5.4.4 C0).
  ['platform', 'The value the newest platform snapshot shows.'],
  ['decided', 'The value of the latest decision recorded for this subject.'],
]);

/**
 * Returns the help text of a metric key, for the `Tooltip` beside the metric's label.
 *
 * The lookup is exact: keys are the backend's machine keys (`Metric.key`), so no case folding
 * or trimming applies, and a key of an object's prototype, such as `toString`, has no entry.
 *
 * @param key - The metric's machine key, as frozen in the evidence (`Metric.key`).
 * @returns The help text, or `undefined` when the key has none, so the label renders alone.
 *
 * @example
 * ```ts
 * metricHelp('idle_days');
 * // "Known-budget active days with zero spend: an observed idle day, not an unknown one."
 * metricHelp('clicks'); // undefined
 * ```
 */
export function metricHelp(key: string): string | undefined {
  return HELP.get(key);
}
