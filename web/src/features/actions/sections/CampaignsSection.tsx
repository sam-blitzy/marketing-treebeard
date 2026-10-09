/**
 * `CampaignsSection`: the "Campaigns to pause or launch" section of `/actions` (AAP 0.5.5,
 * 0.5.4.4; FR-5, FR-10).
 *
 * The section renders inside {@link SectionFrame}, which owns the findings, the group regions,
 * the held disclosure, the empty state, the withheld list and the suppression counts. The groups
 * arrive from the API in their fixed order, Discrepancies, Pause, Scale and Launch ideas, and
 * are never reordered here; within a group the cards keep the API's rank order, and C0 is
 * pinned first by the run itself (AAP 0.5.4.2 P1). Every card is a {@link RecommendationCard}
 * at heading level 4, below its group's `<h3>`, carrying the field set its rule calls for
 * (AAP 0.5.5 "Field sets per section"):
 *
 * - **C0 discrepancy**: "Platform shows {platform}, last decided {decided}", both sides exactly
 *   as the backend rendered them, and in the footer the resolution controls of the live
 *   discrepancy: exactly the resolutions `GET /discrepancies` lists for its kind and subject
 *   type, never derived here (AAP 0.5.4.4). C0 is exempt from dismiss, snooze and done, so the
 *   card shows these controls instead of feedback actions, disabled on a historical item. C0 is
 *   exempt from holds too, so a historical C0 stays pinned in place rather than collapsed with
 *   the held cards.
 * - **C1 Pause, C1b Pipeline pause, C2 Scale or Hold**: a "Campaign evidence" list of spend,
 *   outcomes, expected outcomes and CRM Qualified Leads, each metric that the evidence carries.
 * - **C1b and C2 "Hold"**: a one-row "Pipeline evidence" table of matured leads, matched and
 *   unresolved matured leads, eligible contacts, MQL, QL and meetings, MQL and QL in separate
 *   columns and never combined (AAP 0.5.4.7).
 * - **C3a and C3b launch ideas**: the gap evidence. A segment gap lists the segment with its
 *   leads, cost per lead and impression share; a keyword-cluster gap lists the head tokens and a
 *   "Cluster member terms" table of each member term's spend and conversions.
 *
 * The "Record decision" prefill of C1, C1b and C2 "Scale" is the card's own
 * (`decisionContextFor`), so this section passes no decision context.
 *
 * Presentation rules this file keeps:
 * - Every label, number and sentence comes from the API through the guarded readers in
 *   `../evidenceExtra`: metric labels as the backend wrote them, values through `formatMetric`
 *   (floors "≥ N" and "≤ X", "unavailable" never 0), and no threshold or window length written
 *   into the client (AAP 0.5.4.1). A metric the evidence lacks is left out, never invented.
 * - Server state goes only through `../../../api/queries` (AAP 0.11.1). Only C0 cards read
 *   `GET /discrepancies`, so a section without a discrepancy sends no request; TanStack Query
 *   shares the one read across every C0 card.
 * - Built only on the design-system index (AAP 0.4.4): no raw buttons, headings or tables, no
 *   inline styles and no stylesheet. No `role="alert"`: the open-discrepancies banner in
 *   `GlobalBanners` is the only alert on the page (AAP 0.5.5 Behaviour).
 *
 * @example
 * ```tsx
 * <CampaignsSection section={page.sections[1]} run={page.run} overlay={page.overlay} />
 * ```
 *
 * @packageDocumentation
 */
import { Fragment } from 'react';
import type { ReactElement } from 'react';

import { useDiscrepancies } from '../../../api/queries';
import type { Group, Item, Metric, Overlay } from '../../../api/types';
import { DataTable, Inline, Spinner, Stack } from '../../../design-system';
import type { DataTableColumn } from '../../../design-system';
import { formatCount, formatMetric, formatMoney } from '../../../lib/format';
import { DiscrepancyResolution } from '../../decisions/DiscrepancyResolver';
import { RecommendationCard } from '../RecommendationCard';
import {
  PAUSE_SCALE_METRIC_KEYS,
  PIPELINE_METRIC_KEYS,
  discrepancyRef,
  evidenceCurrency,
  launchGap,
  metricsByKeys,
  scaleOutcome,
} from '../evidenceExtra';
import type { DiscrepancyRef, LaunchGap, LaunchGapTerm } from '../evidenceExtra';
import { FieldList, HeldDisclosure, SectionFrame, isHeld, isHidden } from './SectionFrame';
import type { FieldItem, SectionComponentProps } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/** The accessible name of a C1, C1b or C2 card's list of spend, outcomes and CRM QL. */
export const CAMPAIGN_EVIDENCE_LABEL = 'Campaign evidence';

/** The caption of a C1b or C2 "Hold" card's table of pipeline counts (AAP 0.5.5). */
export const PIPELINE_EVIDENCE_CAPTION = 'Pipeline evidence';

/** The caption of a C3b card's table of the cluster's member search terms. */
export const CLUSTER_TERMS_CAPTION = 'Cluster member terms';

/** The accessible name of a C3a card's segment-gap list. */
export const SEGMENT_GAP_LABEL = 'Segment gap';

/** The accessible name of a C3b card's cluster-gap list. */
export const CLUSTER_GAP_LABEL = 'Cluster gap';

/** The `Spinner` label while a C0 card reads its live discrepancy. */
export const LOADING_DISCREPANCY_LABEL = 'Loading discrepancy';

/**
 * Shown in place of the resolution controls when the C0 card's discrepancy is no longer open
 * or awaiting the platform: it was resolved after the run was computed, so nothing is left to
 * resolve.
 */
export const RESOLVED_SINCE_RUN_TEXT = 'Resolved since this run';

/** Shown in place of the resolution controls when a C0 item names no discrepancy id. */
export const DISCREPANCY_UNAVAILABLE_TEXT = 'Discrepancy details unavailable';

/**
 * The start of the text shown when the live discrepancy list could not be read:
 * "Could not load this discrepancy: {message}".
 */
export const DISCREPANCY_LOAD_ERROR_PREFIX = 'Could not load this discrepancy';

/** The text of the empty discrepancy group's link to the `/decisions` resolver. */
export const RESOLVE_ON_DECISIONS_LABEL = 'Resolve on the Decisions page';

/** The error detail when a failed discrepancy read carries no message of its own. */
const LOAD_ERROR_FALLBACK = 'the request failed';

/** The `/decisions` route, where `DiscrepancyResolver` lists every unresolved discrepancy. */
const DECISIONS_HREF = '/decisions';

/** The Campaigns section's discrepancy group key (`analysis.context.GROUP_DISCREPANCIES`). */
const DISCREPANCY_GROUP_KEY = 'discrepancies';

/** The label of a C3a card's segment field: "Segment: {facet}: {value}". */
const SEGMENT_FIELD_LABEL = 'Segment';

/** The label of a C3b card's head-token field. */
const HEAD_TOKENS_FIELD_LABEL = 'Head tokens';

/** The separator between a C3b cluster's head tokens. */
const HEAD_TOKEN_SEPARATOR = ', ';

/**
 * The C3a metric keys a segment gap lists after the segment, in order: the segment's leads,
 * its cost per lead and its share of the campaign's impressions, as
 * `campaigns._c3a_candidate` writes them. The card's evidence table shows every C3a metric; this
 * list is the gap's headline numbers.
 */
const SEGMENT_GAP_METRIC_KEYS = ['segment_leads', 'segment_cpl', 'impression_share'] as const;

/** The rule ids this section gives a field set (AAP 0.5.4.4). */
const RULE = Object.freeze({
  discrepancy: 'C0',
  pause: 'C1',
  pipelinePause: 'C1b',
  scaleOrHold: 'C2',
  segmentLaunch: 'C3a',
  clusterLaunch: 'C3b',
} as const);

// ---------------------------------------------------------------------------------------------
// Text helpers
// ---------------------------------------------------------------------------------------------

/** Trimmed text, or `null` for a missing or blank value, so "null" is never rendered. */
function textOrNull(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * A C0 card's comparison line: "Platform shows {platform}, last decided {decided}", both sides
 * as the backend rendered them (`render_value`), never reformatted here.
 */
function comparisonText(ref: DiscrepancyRef): string {
  return `Platform shows ${ref.platform}, last decided ${ref.decided}`;
}

/**
 * The live count of unresolved discrepancies, or `0` when the overlay carries no positive
 * whole number, so the empty discrepancy group never reports a count that is not a real one.
 */
function liveDiscrepancyCount(overlay: Overlay): number {
  const count = overlay.open_discrepancies;
  return Number.isInteger(count) && count > 0 ? count : 0;
}

/**
 * The empty discrepancy group's notice for a live count: "1 open discrepancy detected since
 * this run was computed", "3 open discrepancies detected since this run was computed".
 */
function lateDiscrepancyText(count: number): string {
  const noun = count === 1 ? 'discrepancy' : 'discrepancies';
  return `${formatCount(count)} open ${noun} detected since this run was computed`;
}

// ---------------------------------------------------------------------------------------------
// Metric fields
// ---------------------------------------------------------------------------------------------

/**
 * The metrics the evidence carries, as labelled fields in the order given: the backend's label
 * and the value through `formatMetric`. A key the evidence lacks (`null`) is left out.
 */
function metricFields(metrics: readonly (Metric | null)[]): FieldItem[] {
  const fields: FieldItem[] = [];
  for (const metric of metrics) {
    if (metric !== null) {
      fields.push({ key: metric.key, label: metric.label, value: formatMetric(metric) });
    }
  }
  return fields;
}

// ---------------------------------------------------------------------------------------------
// Pause, scale and hold evidence (C1, C1b, C2)
// ---------------------------------------------------------------------------------------------

/** Props of the per-item evidence components. */
interface ItemProps {
  /** The item, as `GET /recommendations` returned it. */
  item: Item;
}

/**
 * The "Campaign evidence" list of a C1, C1b or C2 card (AAP 0.5.5: "Pause and scale cards show
 * spend, outcomes, expected outcomes and CRM QL"): the `PAUSE_SCALE_METRIC_KEYS` metrics, in
 * that order. C1b records no outcomes or expected outcomes, and CRM QL exists only when the
 * campaign is linked to the CRM, so each absent metric is left out.
 */
function CampaignEvidence({ item }: ItemProps): ReactElement | null {
  return (
    <FieldList
      label={CAMPAIGN_EVIDENCE_LABEL}
      fields={metricFields(metricsByKeys(item, PAUSE_SCALE_METRIC_KEYS))}
    />
  );
}

/** The single row of the pipeline table; its cells read the item's metrics directly. */
interface PipelineRow {
  /** The row key. */
  readonly key: string;
}

/** The pipeline table's one row. */
const PIPELINE_ROWS: readonly PipelineRow[] = Object.freeze([{ key: 'pipeline' }]);

/** The `PIPELINE_METRIC_KEYS` key of the matured-population count (`_veto_metrics`). */
const MATURED_LEADS_KEY = 'matured_leads';

/**
 * The header of one pipeline column: the backend's label, except for the matured count, which
 * is headed "Matured {unit}" ("Matured leads" for LinkedIn, "Matured contacts" for Google).
 *
 * The backend's matured label also states the maturity rule ("Matured leads (at least 14 days
 * before window end)"). The design system keeps every table header on one line, so that one
 * label would widen the side-by-side table past a desktop card and push the QL and meetings
 * columns out of view. The rule stays on the card in its evidence table, beside the same count,
 * and the header names the quantity alone in the backend's own unit. Without a unit the
 * backend's label is kept.
 */
function pipelineHeader(metric: Metric): string {
  const unit = textOrNull(metric.unit);
  return metric.key === MATURED_LEADS_KEY && unit !== null ? `Matured ${unit}` : metric.label;
}

/**
 * The one-row "Pipeline evidence" table of a C1b or C2 "Hold" card (AAP 0.5.5: "Hold cards show
 * matured leads, matched and unresolved matured leads, eligible contacts, MQL, QL and
 * meetings"). One end-aligned column per `PIPELINE_METRIC_KEYS` metric, in that order, headed
 * with the backend's label ({@link pipelineHeader}), so MQL carries its bound stage label ("MQL
 * (First meeting booked)"). MQL and QL are two separate columns; no column or total combines them
 * (AAP 0.5.4.7). A metric the evidence lacks gets no column, and with none the table is left
 * out.
 */
function PipelineEvidence({ item }: ItemProps): ReactElement | null {
  const metrics = metricsByKeys(item, PIPELINE_METRIC_KEYS).filter(
    (metric): metric is Metric => metric !== null,
  );
  if (metrics.length === 0) {
    return null;
  }
  const columns: DataTableColumn<PipelineRow>[] = metrics.map((metric) => ({
    key: metric.key,
    header: pipelineHeader(metric),
    align: 'end',
    render: () => formatMetric(metric),
  }));
  return (
    <DataTable
      caption={PIPELINE_EVIDENCE_CAPTION}
      columns={columns}
      rows={PIPELINE_ROWS}
      getRowKey={(row) => row.key}
    />
  );
}

// ---------------------------------------------------------------------------------------------
// Launch gaps (C3a, C3b)
// ---------------------------------------------------------------------------------------------

/** The "{facet}: {value}" field of a segment gap, leaving out a part the evidence lacks. */
function segmentField(gap: LaunchGap): FieldItem {
  const parts = [gap.facet, gap.value].filter((part): part is string => part !== null);
  return { key: 'segment', label: SEGMENT_FIELD_LABEL, value: parts.join(': ') };
}

/** One row of the cluster member table: a member term and its stable row key. */
interface ClusterTermRow {
  /** Unique within the table: the term's position and text. */
  readonly key: string;
  /** The member term with its spend and conversions. */
  readonly term: LaunchGapTerm;
}

/** Props of {@link ClusterTermsTable}. */
interface ClusterTermsTableProps {
  /** The cluster's member terms, in the backend's order (conversions, then clicks). */
  terms: readonly LaunchGapTerm[];
  /** The currency of the terms' spend: the account currency of the item's evidence. */
  currency: string | null;
}

/**
 * The "Cluster member terms" table of a C3b card: the term as the row header, then its window
 * spend in the account currency and its conversions, which stay fractional under data-driven
 * attribution (AAP 0.2.4). An absent amount or count renders "unavailable", never 0.
 */
function ClusterTermsTable({ terms, currency }: ClusterTermsTableProps): ReactElement | null {
  if (terms.length === 0) {
    return null;
  }
  const rows: ClusterTermRow[] = terms.map((term, index) => ({
    key: `${String(index)}:${term.term}`,
    term,
  }));
  const columns: DataTableColumn<ClusterTermRow>[] = [
    { key: 'term', header: 'Term', isRowHeader: true, render: (row) => row.term.term },
    {
      key: 'spend',
      header: 'Spend',
      align: 'end',
      render: (row) => formatMoney(row.term.spend, currency),
    },
    {
      key: 'conversions',
      header: 'Conversions',
      align: 'end',
      render: (row) => formatCount(row.term.conversions),
    },
  ];
  return (
    <DataTable
      caption={CLUSTER_TERMS_CAPTION}
      columns={columns}
      rows={rows}
      getRowKey={(row) => row.key}
    />
  );
}

/**
 * The gap evidence of a launch idea (AAP 0.5.5: "Launch cards show the gap evidence"):
 *
 * - a segment gap (C3a): the "Segment gap" list of "Segment: {facet}: {value}" followed by the
 *   segment's leads, cost per lead and impression share, each when the evidence carries it;
 * - a keyword-cluster gap (C3b): the "Cluster gap" list of the head tokens, when there are any,
 *   followed by the {@link ClusterTermsTable};
 * - no gap: nothing beyond the card's own reason and evidence table.
 */
function LaunchGapEvidence({ item }: ItemProps): ReactElement | null {
  const gap = launchGap(item);
  if (gap.kind === 'segment') {
    return (
      <FieldList
        label={SEGMENT_GAP_LABEL}
        fields={[segmentField(gap), ...metricFields(metricsByKeys(item, SEGMENT_GAP_METRIC_KEYS))]}
      />
    );
  }
  if (gap.kind === 'cluster') {
    return (
      <>
        {gap.headTokens.length === 0 ? null : (
          <FieldList
            label={CLUSTER_GAP_LABEL}
            fields={[
              {
                key: 'head_tokens',
                label: HEAD_TOKENS_FIELD_LABEL,
                value: gap.headTokens.join(HEAD_TOKEN_SEPARATOR),
              },
            ]}
          />
        )}
        <ClusterTermsTable terms={gap.terms} currency={evidenceCurrency(item)} />
      </>
    );
  }
  return null;
}

// ---------------------------------------------------------------------------------------------
// Discrepancies (C0)
// ---------------------------------------------------------------------------------------------

/** Props of {@link LiveDiscrepancyResolution}. */
interface LiveDiscrepancyResolutionProps {
  /** The discrepancy id the C0 item froze (`extra.discrepancy_id`), as a string. */
  discrepancyId: string;
  /** Disables every resolution, as on a historical item computed from replaced data. */
  disabled: boolean;
}

/**
 * The resolution controls of a C0 card, read from the live discrepancy list
 * (`GET /discrepancies`, open and awaiting-platform), so the buttons are exactly the
 * resolutions the API offers for the discrepancy's kind and subject type now (AAP 0.5.4.4):
 *
 * - while the first read runs: a `Spinner` "Loading discrepancy";
 * - when it fails before any list arrived: "Could not load this discrepancy: {message}" and no
 *   control;
 * - when the list holds the discrepancy: its {@link DiscrepancyResolution}, disabled when
 *   `disabled` is set;
 * - when it does not: "Resolved since this run" and no control.
 *
 * A failed refresh of a list already read keeps that list, because the server refuses a
 * resolution of a discrepancy closed since then with its own message.
 */
function LiveDiscrepancyResolution({
  discrepancyId,
  disabled,
}: LiveDiscrepancyResolutionProps): ReactElement {
  const discrepancies = useDiscrepancies();
  const list = discrepancies.data;
  if (list === undefined) {
    if (discrepancies.isError) {
      const message = textOrNull(discrepancies.error.message) ?? LOAD_ERROR_FALLBACK;
      return <div>{`${DISCREPANCY_LOAD_ERROR_PREFIX}: ${message}`}</div>;
    }
    return <Spinner label={LOADING_DISCREPANCY_LABEL} />;
  }
  const discrepancy = list.find((entry) => String(entry.id) === discrepancyId);
  if (discrepancy === undefined) {
    return <div>{RESOLVED_SINCE_RUN_TEXT}</div>;
  }
  return <DiscrepancyResolution discrepancy={discrepancy} disabled={disabled} />;
}

/**
 * A C0 discrepancy card: the comparison line in the body and the live resolution controls in
 * the footer, where the card puts them instead of feedback actions (AAP 0.5.4.2: C0 cannot be
 * dismissed, snoozed or marked done). A C0 item without a discrepancy id shows "Discrepancy
 * details unavailable" in the footer instead, because no discrepancy can be looked up.
 */
function DiscrepancyCard({ item }: ItemProps): ReactElement {
  const ref = discrepancyRef(item);
  return (
    <RecommendationCard
      item={item}
      headingLevel={4}
      resolver={({ disabled }) =>
        ref === null ? (
          <div>{DISCREPANCY_UNAVAILABLE_TEXT}</div>
        ) : (
          <LiveDiscrepancyResolution discrepancyId={ref.discrepancyId} disabled={disabled} />
        )
      }
    >
      {ref === null ? null : <div>{comparisonText(ref)}</div>}
    </RecommendationCard>
  );
}

/**
 * The notice an empty discrepancy group shows while discrepancies detected after the run are
 * open: the live count and a link to the `/decisions` resolver, where they can be resolved
 * before the rerun lists them here.
 */
function LateDiscrepancyNotice({ count }: { count: number }): ReactElement {
  return (
    <Inline gap={2}>
      <span>{lateDiscrepancyText(count)}</span>
      <a href={DECISIONS_HREF}>{RESOLVE_ON_DECISIONS_LABEL}</a>
    </Inline>
  );
}

// ---------------------------------------------------------------------------------------------
// The section
// ---------------------------------------------------------------------------------------------

/**
 * One card of the section, with the field set its rule calls for (see the module overview).
 * A rule this section does not know renders the plain card, so no stored item is dropped.
 */
function CampaignCard({ item }: ItemProps): ReactElement {
  switch (item.rule_id) {
    case RULE.discrepancy:
      return <DiscrepancyCard item={item} />;
    case RULE.pause:
      return (
        <RecommendationCard item={item} headingLevel={4}>
          <CampaignEvidence item={item} />
        </RecommendationCard>
      );
    case RULE.pipelinePause:
      return (
        <RecommendationCard item={item} headingLevel={4}>
          <CampaignEvidence item={item} />
          <PipelineEvidence item={item} />
        </RecommendationCard>
      );
    case RULE.scaleOrHold:
      // "Hold" (the pipeline veto fired) is told apart by the frozen outcome, not by the
      // missing decision subject: a Scale item keeps its subject even when the overlay holds it.
      return (
        <RecommendationCard item={item} headingLevel={4}>
          <CampaignEvidence item={item} />
          {scaleOutcome(item) === 'Hold' ? <PipelineEvidence item={item} /> : null}
        </RecommendationCard>
      );
    case RULE.segmentLaunch:
    case RULE.clusterLaunch:
      return (
        <RecommendationCard item={item} headingLevel={4}>
          <LaunchGapEvidence item={item} />
        </RecommendationCard>
      );
    default:
      return <RecommendationCard item={item} headingLevel={4} />;
  }
}

/** Props of {@link DiscrepancyGroupBody}. */
interface DiscrepancyGroupBodyProps {
  /** The discrepancy group, as the API returned it. */
  group: Group;
  /** The group's anchor id, from which the held region's id is derived. */
  anchorId: string;
}

/**
 * The body of the discrepancy group: every C0 card in API order (oldest `detected_at` first),
 * none of them behind the held disclosure, then any other held item behind it.
 *
 * C0 is exempt from holds and pinned first (AAP 0.5.4.2 P1), yet the overlay sets `held_now` on
 * every item computed from replaced data (AAP 0.3.2), C0 included. The frame's default body
 * would then collapse a historical C0 under "Held until discrepancies are resolved", a hold C0
 * never has, while AAP 0.5.5 shows it in place with its resolver disabled. So C0 cards stay
 * pinned here, and the card itself shows the historical badge and disables the resolver. Items
 * feedback hides are skipped, as the frame does (the frame counts them).
 */
function DiscrepancyGroupBody({ group, anchorId }: DiscrepancyGroupBodyProps): ReactElement {
  const shown = group.items.filter((item) => !isHidden(item));
  const pinned = shown.filter((item) => item.rule_id === RULE.discrepancy || !isHeld(item));
  const held = shown.filter((item) => item.rule_id !== RULE.discrepancy && isHeld(item));
  return (
    <Stack>
      {pinned.map((item) => (
        <Fragment key={item.id}>
          <CampaignCard item={item} />
        </Fragment>
      ))}
      <HeldDisclosure id={`${anchorId}-held`} count={held.length}>
        {held.map((item) => (
          <Fragment key={item.id}>
            <CampaignCard item={item} />
          </Fragment>
        ))}
      </HeldDisclosure>
    </Stack>
  );
}

/**
 * The "Campaigns to pause or launch" section (AAP 0.5.5): Discrepancies, Pause, Scale and Launch
 * ideas, in the API's order, each card with its rule's field set (see the module overview).
 *
 * C0 cards stay pinned at the top of the discrepancy group even when the overlay marks them
 * historical ({@link DiscrepancyGroupBody}); every other group uses the frame's default body.
 *
 * The discrepancy group stays on the page while it is empty in the run but the overlay counts
 * open discrepancies now: the open-discrepancies banner and every held card link to its anchor
 * (`#group-campaigns-discrepancies`), and a discrepancy detected after the run holds items at
 * once (AAP 0.5.4.2 P1). The group then shows the live count and a link to `/decisions`. Every
 * other empty group is left out, as `SectionFrame` does by default.
 *
 * @param props - see `SectionComponentProps`; `run` is not read, because every window figure
 *   the cards show comes from their own frozen evidence
 * @returns the section region
 *
 * @example
 * ```tsx
 * <CampaignsSection section={campaigns} run={page.run} overlay={page.overlay} />
 * ```
 */
export function CampaignsSection({ section, overlay }: SectionComponentProps): ReactElement {
  const openNow = liveDiscrepancyCount(overlay);
  return (
    <SectionFrame
      section={section}
      renderItem={(item) => <CampaignCard item={item} />}
      renderGroup={(group, parts) =>
        group.key === DISCREPANCY_GROUP_KEY ? (
          <DiscrepancyGroupBody group={group} anchorId={parts.anchorId} />
        ) : (
          parts.body
        )
      }
      // The run's discrepancy group can be empty while discrepancies detected after it are
      // open; it is kept, with a notice instead of cards, so the banner's and held cards'
      // links never point at nothing. The notice reads no list, so a section without C0 cards
      // sends no GET /discrepancies.
      renderEmptyGroup={(group) =>
        group.key === DISCREPANCY_GROUP_KEY && openNow > 0 ? (
          <LateDiscrepancyNotice count={openNow} />
        ) : null
      }
    />
  );
}
