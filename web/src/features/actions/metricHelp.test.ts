// @vitest-environment node
/**
 * Unit tests for src/features/actions/metricHelp.ts, the help text of the metrics a
 * recommendation cites (AAP 0.4.2 "Help text on metrics").
 *
 * The suite holds the module to its TSDoc contract: every backend metric key it documents has a
 * short, plain text; no text writes a configured value as a literal (AAP 0.5.4.1: the UI renders
 * configured values, never literals), so no text holds a digit; the texts say what the product's
 * definitions say for the keys they cover; and a key with no entry, a blank key, a key in
 * another case and a key of an object's prototype all return `undefined`. The module is pure,
 * so the suite runs in Node.
 */
import { describe, expect, it } from 'vitest';

import { metricHelp } from './metricHelp';

/**
 * Every backend metric key with help text, as `backend/src/ads_engine/analysis/*.py` emits it:
 * the shared window, outcome and efficiency keys, the budget-day counts and shares, the CRM
 * pipeline counts, the white-paper attribution counts and costs, the creative stage rates with
 * their interval bounds and test sizes, and the keyword, launch-segment and discrepancy keys.
 */
const HELPED_KEYS = [
  'window_days',
  'outcomes',
  'cost_per_outcome',
  'expected_outcomes',
  'window_utilization',
  'known_days',
  'spending_days',
  'spending_day_share',
  'idle_days',
  'changed_intraday_days',
  'unobserved_days',
  'capped_days',
  'capped_days_share',
  'high_utilization_days',
  'high_utilization_days_share',
  'under_utilized_days',
  'under_utilized_days_share',
  'spend_days',
  'spend_day_share',
  'matured_leads',
  'matched_leads',
  'unresolved',
  'unresolved_share',
  'eligible_contacts',
  'unknown_evidence_share',
  'mql',
  'ql',
  'meetings',
  'young_leads',
  'young_ql',
  'linkedin_leads',
  'meetings_booked',
  'other_meetings',
  'deals_after_touch',
  'cost_per_ql',
  'cost_per_meeting',
  'click_rate',
  'open_rate',
  'completion_rate',
  'click_rate_ci_low',
  'click_rate_ci_high',
  'open_rate_ci_low',
  'open_rate_ci_high',
  'completion_rate_ci_low',
  'completion_rate_ci_high',
  'expected_min_cell',
  'additional_impressions_per_variant',
  'sample_per_variant',
  'projected_impressions_per_variant',
  'projected_days',
  'conversions',
  'cost_per_conversion',
  'wasted_spend',
  'min_clicks',
  'qs_first',
  'qs_last',
  'cpc_ratio',
  'cpa_ratio',
  'segment_cpl',
  'campaign_cpl',
  'cpl_ratio',
  'impression_share',
  'platform',
  'decided',
] as const;

/** Backend keys whose label already says everything, or whose meaning differs between rules. */
const UNHELPED_KEYS = [
  'spend',
  'clicks',
  'leads',
  'daily_budget',
  'proposed_daily_budget',
  'increase',
  'freed',
  'held_recommendations',
] as const;

describe('metricHelp', () => {
  it.each(HELPED_KEYS)('has a short, plain help text for %s', (key) => {
    const help = metricHelp(key);
    expect(help).toBeTypeOf('string');
    const text = help ?? '';
    expect(text.trim()).toBe(text);
    expect(text.length).toBeGreaterThan(0);
    // Short enough to read in a tooltip bubble.
    expect(text.length).toBeLessThanOrEqual(320);
    // A full sentence, never a fragment.
    expect(text).toMatch(/[.!?]$/u);
  });

  it.each(HELPED_KEYS)('writes no configured value as a literal in the help of %s', (key) => {
    // Thresholds, periods and levels come from configuration (AAP 0.5.4.1), so a text names
    // "the configured …" and never a number.
    expect(metricHelp(key)).not.toMatch(/\d/u);
    expect(metricHelp(key)).not.toMatch(/%/u);
  });

  it.each(UNHELPED_KEYS)('has no help for %s, so its label renders alone', (key) => {
    expect(metricHelp(key)).toBeUndefined();
  });

  it('returns undefined for a blank, unknown, differently cased or prototype key', () => {
    expect(metricHelp('')).toBeUndefined();
    expect(metricHelp(' idle_days ')).toBeUndefined();
    expect(metricHelp('IDLE_DAYS')).toBeUndefined();
    expect(metricHelp('not_a_metric')).toBeUndefined();
    expect(metricHelp('toString')).toBeUndefined();
    expect(metricHelp('constructor')).toBeUndefined();
    expect(metricHelp('__proto__')).toBeUndefined();
    expect(metricHelp('hasOwnProperty')).toBeUndefined();
  });

  it('defines efficiency, expected outcomes and the account baseline as the product does', () => {
    // AAP 0.5.4.1: efficiency is cost per outcome, compared with the account average of the
    // same source and objective group over the same window.
    expect(metricHelp('cost_per_outcome')).toBe(
      'Efficiency: window spend divided by window outcomes. It is compared with the account ' +
        'baseline, the average for the same source and objective group over the same window.',
    );
    expect(metricHelp('expected_outcomes')).toContain(
      'spend divided by the account baseline cost per outcome',
    );
    expect(metricHelp('outcomes')).toContain('Google: conversions');
    expect(metricHelp('outcomes')).toContain('one-click leads');
    expect(metricHelp('outcomes')).toContain('external website conversions');
  });

  it('defines window utilization over known-budget active days, unknown days left out', () => {
    const help = metricHelp('window_utilization') ?? '';
    expect(help).toContain('Window spend divided by the sum of daily budgets');
    expect(help).toContain('known-budget active days');
    expect(help).toContain('budget-active days for a shared budget');
    expect(help).toContain('Days whose budget is unknown are left out');
    expect(help).toContain('daily budgets only');
  });

  it('defines the budget day classes as the observation rule does', () => {
    expect(metricHelp('known_days')).toContain('known for the whole day');
    expect(metricHelp('known_days')).toContain('configured maximum gap');
    expect(metricHelp('spending_days')).toBe('Known-budget active days with spend above zero.');
    expect(metricHelp('idle_days')).toContain('zero spend');
    expect(metricHelp('idle_days')).toContain('not an unknown one');
    expect(metricHelp('changed_intraday_days')).toContain('disagree');
    expect(metricHelp('unobserved_days')).toContain('configured maximum');
    for (const key of ['changed_intraday_days', 'unobserved_days']) {
      expect(metricHelp(key)).toContain('left out of utilization and cap counts');
    }
  });

  it('defines the CRM pipeline counts, keeping unresolved leads in the population', () => {
    expect(metricHelp('matured_leads')).toContain('configured maturity period');
    expect(metricHelp('unresolved')).toContain('pending, no match, no email or error');
    expect(metricHelp('unresolved')).toContain('never dropped');
    expect(metricHelp('unresolved_share')).toContain(
      'Unresolved matured leads divided by all matured leads',
    );
    expect(metricHelp('unresolved_share')).toContain('configured floor share');
    expect(metricHelp('eligible_contacts')).toContain('counts once');
    expect(metricHelp('ql')).toContain('never derived from MQL');
    expect(metricHelp('meetings_booked')).toContain('record creation stands in');
    expect(metricHelp('other_meetings')).toContain('Left out of cost per meeting');
  });

  it('defines each creative stage rate by its own denominator', () => {
    expect(metricHelp('click_rate')).toMatch(/^Clicks divided by impressions/u);
    expect(metricHelp('open_rate')).toMatch(/^Lead form opens divided by impressions/u);
    expect(metricHelp('completion_rate')).toMatch(/^Leads divided by lead form opens/u);
    for (const stage of ['click_rate', 'open_rate', 'completion_rate']) {
      expect(metricHelp(`${stage}_ci_low`)).toContain('Wilson interval');
      expect(metricHelp(`${stage}_ci_high`)).toContain('configured confidence level');
    }
  });
});
