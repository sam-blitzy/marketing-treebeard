"""Window aggregation, budget-day classification, utilization, efficiency and the G1 predicate.

AAP 0.5.1 ("Metrics and statistics": "Window aggregation; utilization, capped days,
efficiency"), 0.5.4.1 (outcome by source, efficiency, expected outcomes, budget history,
utilization, daily predicates, history gates, shared and lifetime budgets), 0.5.4.5 (shared
budgets: dated membership, budget-active days, aggregation, the member-level safety
predicate; the B4 counting contract), 0.5.4.2 (G1 targets) and 0.2.1 / 0.1.1 (performance:
"Window aggregation runs as SQL group-bys"; a custom 180-day run computes in under 30 s).

The module has two halves with a hard boundary between them:

* **SQL half** (:class:`QueryScope`, :func:`aggregate_sql`, :func:`load_context`). The only
  database code in ``analysis/`` besides ``runner``, ``lock`` and ``overlay``. It runs async
  on SQLAlchemy 2.1 and psycopg 3, reads only, and never commits, rolls back or flushes.
  Every query filters to each source's **active primary** dataset (decision 8);
  LinkedIn-attributed CRM reads use the effective CRM dataset of the LinkedIn dataset (the
  companion while LinkedIn is simulated) and the Google-to-CRM link reads HubSpot's active
  primary dataset, so a simulated row never joins a live row within one source.
* **Pure half** (everything else, starting with :func:`aggregate`, the AAP 0.5.1
  ``aggregate(ctx, grain)`` interface). Functions of an
  :class:`~ads_engine.analysis.context.AnalysisContext` with no I/O and no logging, so rule
  modules, ``precedence.py`` and scenario tests use them on contexts built in memory.
  :func:`load_context` fills a context from :func:`aggregate_sql`, one query per grain, and
  :func:`aggregate` reads each grain back from any context, loaded or built in memory. Derived
  tables (budget histories, baselines, the G1 set, the counting contract) are memoised in
  ``ctx.cache`` and are immutable, so every rule of one run reads the same values.

Value conventions (decision 6): money, conversions, ratios and shares are ``Decimal``;
instants are tz-aware; daily facts keep each source's native date (Google in the account's
time zone, LinkedIn in UTC) and are never rebucketed. A value the platform did not report is
``None`` ("unavailable"), never 0, with one exception: where a context type requires a number
and every value an aggregate sums is ``NULL``, :func:`_as_int` and :func:`_as_decimal` read
the sum as 0. That applies only to

* LinkedIn ``cost``, ``impressions`` and ``clicks`` per campaign day (``DayFact``) and per
  creative over the window (``CreativeTotals``);
* ``conversions`` per search term over the window (``SearchTermRow``) and over K1's trailing
  account baseline (``RateBaseline``).

Every other unreported metric stays ``None``, outcomes included: Google campaign-day and
keyword-day ``conversions``, LinkedIn ``leads`` and ``website_conversions``.

Budget history (AAP 0.5.4.1) in one paragraph: snapshots are *observations*, never effective
times. A native day is **whole-day known** only when an observation exists at or before the
day start and at or after the day end, every observation of that span shows the same value,
and no gap between consecutive observations of the span exceeds
``budget.max_observation_gap_hours``. Disagreeing observations make the day **changed
intraday**; a missing bracket or a longer gap makes it **unobserved**. Both are unknown: they
are excluded from every utilization and cap calculation and counted separately in evidence.
The stated assumption is "unchanged across N observations, at most 6 h apart"
(:attr:`BudgetHistory.assumption`).

Example:
    >>> history = campaign_budget_history(ctx, campaign_id=7)  # doctest: +SKIP
    >>> gate = cap_constrained(history)  # doctest: +SKIP
    >>> gate.passed, gate.shortfalls  # doctest: +SKIP
    (False, ('spending days 3 of 5 required',))
"""

from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Final, Literal, get_args, overload

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import aggregate_order_by, distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.analysis.context import (
    AccountInfo,
    AdGroupInfo,
    AnalysisContext,
    AssociationFetchInfo,
    AssociationStatus,
    BudgetInfo,
    BudgetPeriod,
    CampaignGroupInfo,
    CampaignInfo,
    CampaignStatus,
    Completeness,
    ContactInfo,
    ContactReadStatus,
    ConversionActionInfo,
    CreativeInfo,
    CreativeTotals,
    CrmData,
    DatasetMode,
    DatasetRef,
    DatasetRole,
    DayFact,
    DealInfo,
    DecisionInfo,
    DecisionOrigin,
    DecisionShape,
    DiscrepancyInfo,
    DiscrepancyKind,
    FeedbackInfo,
    FeedbackKind,
    Freshness,
    KeywordDay,
    KeywordInfo,
    LeadInfo,
    MatchStatus,
    MeetingInfo,
    Observation,
    OpenDiscrepancyState,
    RateBaseline,
    RunKind,
    SearchTermRow,
    SegmentRow,
    Source,
    SubjectKey,
    SubjectType,
    TargetReadStatus,
    objective_group,
)
from ads_engine.analysis.thresholds import ConfigError, CrmBindings, Thresholds
from ads_engine.analysis.window import UTC_BOUNDARY, AnalysisWindow
from ads_engine.db.models import (
    AdAccount,
    AdGroup,
    Budget,
    Campaign,
    CampaignGroup,
    ConversionAction,
    Creative,
    CrmAssociation,
    CrmAssociationFetch,
    CrmContact,
    CrmDeal,
    CrmMeeting,
    DatasetFreshness,
    Decision,
    Discrepancy,
    EntitySnapshot,
    FactCampaignDaily,
    FactCreativeDaily,
    FactKeywordDaily,
    FactSearchTermDaily,
    FactSegmentWindow,
    Keyword,
    Lead,
    LeadMatch,
    RecommendationFeedback,
    SourceDataset,
)
from ads_engine.db.repositories import active_datasets, effective_crm_dataset_id
from ads_engine.logging import get_logger

__all__ = [
    "B2_THRESHOLD_KEYS",
    "BUDGET_THRESHOLD_KEYS",
    "G1_THRESHOLD_KEYS",
    "GRAINS",
    "LAST_CLICK_MODEL",
    "B2Assessment",
    "B2Branch",
    "BudgetDay",
    "BudgetHistory",
    "CampaignDay",
    "ContractDay",
    "DayClass",
    "GateConfig",
    "GateResult",
    "Grain",
    "QueryScope",
    "Totals",
    "aggregate",
    "aggregate_sql",
    "assess_b2",
    "baseline_cpo",
    "budget_amount_value",
    "budget_history",
    "budget_subjects",
    "campaign_budget_history",
    "campaign_state_value",
    "cap_constrained",
    "classify_days",
    "cost_per_outcome",
    "counting_contract",
    "expected_outcomes",
    "high_utilization",
    "load_context",
    "member_poor_efficiency",
    "membership_value",
    "outcome",
    "shared_budget_history",
    "source_totals",
    "spend_increase_blocked",
    "status_value",
    "subject_baseline",
    "subject_campaigns",
    "subject_objective_groups",
    "subject_totals",
    "under_utilized",
    "window_totals",
]

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_MICROS: Final = Decimal(1_000_000)
"""Google amounts arrive in micros; ``Decimal(micros) / _MICROS`` is exact at 6 places."""

_ZERO: Final = Decimal(0)

_AD_SOURCES: Final[tuple[Literal["google", "linkedin"], ...]] = ("google", "linkedin")
"""The sources with campaigns, daily facts and decision subjects."""

_METRICS_DATASETS: Final[Mapping[str, str]] = MappingProxyType(
    {"google": "google.metrics_daily", "linkedin": "linkedin.analytics_daily"}
)
"""The freshness-tracked daily metrics dataset of each ad source (``dataset_freshness``)."""

_OBSERVED_TYPES: Final[tuple[str, ...]] = ("campaign", "budget", "conversion_action")
"""Snapshot entity types whose full observation history over the window is loaded.

Keywords are loaded separately and sparsely (:func:`_load_keyword_observations`).
"""

_MAX_UTC_OFFSET: Final = timedelta(hours=15)
"""Upper bound on any IANA zone's distance from UTC (14 h) plus one hour of slack."""

_CRM_LINKED_TYPES: Final[tuple[str, ...]] = ("meeting", "deal")
"""The targets a contact's association fetches read (AAP 0.3.3)."""

_CODE_POINT: Final = "C"
"""The collation of every text sort key behind the order of a context's collections.

PostgreSQL's ``"C"`` collation compares UTF-8 bytes, which order exactly as Unicode code
points, the order Python sorts ``str`` in; it exists in every database, whatever the default
locale. ``load_context`` therefore lists rows in one order on any database, and in the order
``simulation.scenarios.context_from_world`` builds the same rows in memory.
"""

_SOURCES_ALL: Final[tuple[Source, ...]] = get_args(Source)
_DATASET_MODES: Final[tuple[DatasetMode, ...]] = get_args(DatasetMode)
_DATASET_ROLES: Final[tuple[DatasetRole, ...]] = get_args(DatasetRole)
_COMPLETENESS: Final[tuple[Completeness, ...]] = get_args(Completeness)
_CAMPAIGN_STATUSES: Final[tuple[CampaignStatus, ...]] = get_args(CampaignStatus)
_BUDGET_PERIODS: Final[tuple[BudgetPeriod, ...]] = get_args(BudgetPeriod)
_SUBJECT_TYPES: Final[tuple[SubjectType, ...]] = get_args(SubjectType)
_DECISION_SHAPES: Final[tuple[DecisionShape, ...]] = get_args(DecisionShape)
_DECISION_ORIGINS: Final[tuple[DecisionOrigin, ...]] = get_args(DecisionOrigin)
_DISCREPANCY_KINDS: Final[tuple[DiscrepancyKind, ...]] = get_args(DiscrepancyKind)
_OPEN_STATES: Final[tuple[OpenDiscrepancyState, ...]] = get_args(OpenDiscrepancyState)
_FEEDBACK_KINDS: Final[tuple[FeedbackKind, ...]] = get_args(FeedbackKind)
_MATCH_STATUSES: Final[tuple[MatchStatus, ...]] = get_args(MatchStatus)
_CONTACT_READ: Final[tuple[ContactReadStatus, ...]] = get_args(ContactReadStatus)
_ASSOCIATION_STATUSES: Final[tuple[AssociationStatus, ...]] = get_args(AssociationStatus)
_TARGET_READ: Final[tuple[TargetReadStatus, ...]] = get_args(TargetReadStatus)
_RUN_KINDS: Final[tuple[RunKind, ...]] = get_args(RunKind)


def _choice[T: str](value: object, allowed: tuple[T, ...], what: str) -> T:
    """Return ``value`` typed as one of ``allowed``.

    Database ``CHECK`` constraints already restrict these columns; this keeps the typed
    context honest if a row ever disagrees.

    Raises:
        ValueError: When ``value`` is not one of ``allowed``.
    """
    for option in allowed:
        if value == option:
            return option
    raise ValueError(f"{what} must be one of {allowed}, found {value!r}")


def _optional_choice[T: str](value: object, allowed: tuple[T, ...], what: str) -> T | None:
    """Return ``None`` for ``None``, otherwise :func:`_choice`."""
    return None if value is None else _choice(value, allowed, what)


def _as_int(value: object) -> int:
    """Return a SQL integer or ``numeric`` value as ``int``; ``NULL`` reads as 0.

    Used for ``NOT NULL`` columns, ``COUNT`` results and ``SUM`` aggregates. A ``SUM`` is
    ``NULL`` only when every summed value is ``NULL``; the 0 then stands in where the context
    type requires a number (LinkedIn impressions and clicks per campaign day and per creative
    over the window). Use :func:`_as_optional_int` where an unreported value stays ``None``.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise TypeError(f"expected an integer aggregate, found {value!r}")
    return int(value)


def _as_optional_int(value: object) -> int | None:
    """Return ``None`` for ``NULL`` (unreported), else :func:`_as_int`."""
    return None if value is None else _as_int(value)


def _as_decimal(value: object) -> Decimal:
    """Return a SQL ``numeric`` value as ``Decimal``; ``NULL`` reads as 0.

    Used for ``NOT NULL`` columns and ``SUM`` aggregates. A ``SUM`` is ``NULL`` only when every
    summed value is ``NULL``; the 0 then stands in where the context type requires a number
    (LinkedIn cost per campaign day and per creative over the window, and ``conversions`` per
    search term and over K1's trailing baseline). Use :func:`_as_optional_decimal` where an
    unreported value stays ``None``.
    """
    if value is None:
        return _ZERO
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise TypeError(f"expected a numeric value, found {value!r}")
    return Decimal(value)


def _as_optional_decimal(value: object) -> Decimal | None:
    """Return ``None`` for ``NULL`` (unreported), else :func:`_as_decimal`."""
    return None if value is None else _as_decimal(value)


# ---------------------------------------------------------------------------
# SQL half: scope and window aggregation (AAP 0.5.1, 0.3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryScope:
    """What one context load reads: the session, the window and the dataset ids to filter to.

    Attributes:
        session: The caller's session; never committed, rolled back or flushed here.
        window: The analysis window; every daily query is bounded by its native dates.
        thresholds: The loaded configuration (``k1.baseline_days``,
            ``budget.max_observation_gap_hours``).
        datasets: Each source's active primary dataset; a source without one is absent and
            its queries return nothing.
        linkedin_crm_id: The effective CRM dataset of the active LinkedIn dataset (the
            companion while LinkedIn is simulated, else HubSpot's active primary).
        google_crm_id: HubSpot's active primary dataset, which the Google-to-CRM link reads.
        now: The run time; window deals are those created up to this instant.
    """

    session: AsyncSession
    window: AnalysisWindow
    thresholds: Thresholds
    datasets: Mapping[Source, DatasetRef]
    linkedin_crm_id: int | None
    google_crm_id: int | None
    now: datetime

    def dataset_id(self, source: Source) -> int | None:
        """Return the active primary dataset id of ``source``, or ``None`` when it has none."""
        ref = self.datasets.get(source)
        return None if ref is None else ref.dataset_id

    def ad_dataset_ids(self) -> list[int]:
        """Return the active Google and LinkedIn dataset ids, in that order."""
        return [ds for ds in (self.dataset_id("google"), self.dataset_id("linkedin")) if ds]


Grain = Literal[
    "campaign_day_google",
    "campaign_day_linkedin",
    "creative_window",
    "keyword_day",
    "search_term_window",
    "google_rate_baseline",
    "segment_window",
]
"""The aggregation grains: :func:`aggregate_sql` computes each in SQL and :func:`aggregate`
reads each back from an :class:`AnalysisContext`."""

GRAINS: Final[tuple[Grain, ...]] = get_args(Grain)
"""Every :data:`Grain`, in the order :func:`load_context` reads them from
:func:`aggregate_sql`."""


@dataclass(frozen=True, slots=True)
class CampaignDay:
    """One campaign's facts on one native date: a row of a ``campaign_day_*`` grain.

    :func:`aggregate_sql` returns these rows from SQL and :func:`aggregate` from a context.

    Attributes:
        campaign_id: The campaign (of the grain's source dataset).
        fact: The day's facts.
    """

    campaign_id: int
    fact: DayFact


def _int_sum(column: Any) -> sa.Cast[int]:
    """``CAST(SUM(column) AS bigint)``: PostgreSQL sums ``bigint`` as ``numeric``."""
    return sa.cast(sa.func.sum(column), sa.BigInteger)


@overload
async def aggregate_sql(
    scope: QueryScope, grain: Literal["campaign_day_google", "campaign_day_linkedin"]
) -> list[CampaignDay]: ...
@overload
async def aggregate_sql(
    scope: QueryScope, grain: Literal["creative_window"]
) -> list[CreativeTotals]: ...
@overload
async def aggregate_sql(scope: QueryScope, grain: Literal["keyword_day"]) -> list[KeywordDay]: ...
@overload
async def aggregate_sql(
    scope: QueryScope, grain: Literal["search_term_window"]
) -> list[SearchTermRow]: ...
@overload
async def aggregate_sql(
    scope: QueryScope, grain: Literal["google_rate_baseline"]
) -> list[RateBaseline]: ...
@overload
async def aggregate_sql(
    scope: QueryScope, grain: Literal["segment_window"]
) -> list[SegmentRow]: ...
@overload
async def aggregate_sql(scope: QueryScope, grain: Grain) -> list[Any]: ...
async def aggregate_sql(scope: QueryScope, grain: Grain) -> list[Any]:
    """Aggregate one grain of the active datasets over the window in SQL.

    AAP 0.2.1 ("Window aggregation runs as SQL group-bys"): the database source of every
    grain. :func:`load_context` stores each grain's rows in the context it builds, and the
    pure :func:`aggregate` (AAP 0.5.1 ``aggregate(ctx, grain)``) reads the same rows back
    from that context in this order. Every daily grain is bounded by ``window.start`` to
    ``window.end`` on its table's native ``date`` column; nothing is rebucketed.

    * ``campaign_day_google``: ``fact_campaign_daily`` rows, one per campaign per Google date
      (the natural key already makes each (campaign, date) a single row).
    * ``campaign_day_linkedin``: ``fact_creative_daily`` summed per ``creative.campaign_id``
      per UTC date. ``SUM`` semantics: an optional metric is ``None`` only when every summed
      value is ``NULL``; cost, impressions and clicks, which :class:`DayFact` requires, sum
      an all-``NULL`` group to 0.
    * ``creative_window``: window totals per creative, with ``delivery_days`` = dates with
      impressions > 0.
    * ``keyword_day``: ``fact_keyword_daily`` rows.
    * ``search_term_window``: ``fact_search_term_daily`` summed per (ad group, campaign,
      matched criterion, term); ``term_status``, the matched keyword text and match type are
      those of the latest date in the window. A stored ``''`` criterion (no keyword segment)
      becomes ``None``.
    * ``google_rate_baseline``: account ``SUM(conversions)`` and ``SUM(clicks)`` over the
      trailing ``k1.baseline_days`` ending at ``window.end``; empty when no row exists.
    * ``segment_window``: LinkedIn segment rows whose window is exactly this run's window.

    Args:
        scope: The session, window and active dataset ids.
        grain: The grain to compute.

    Returns:
        The grain's rows, ordered deterministically; empty when the grain's source has no
        active dataset.

    Raises:
        ValueError: For an unknown grain.
    """
    match grain:
        case "campaign_day_google":
            return await _campaign_days_google(scope)
        case "campaign_day_linkedin":
            return await _campaign_days_linkedin(scope)
        case "creative_window":
            return await _creative_window(scope)
        case "keyword_day":
            return await _keyword_days(scope)
        case "search_term_window":
            return await _search_term_window(scope)
        case "google_rate_baseline":
            return await _google_rate_baseline(scope)
        case "segment_window":
            return await _segment_window(scope)
    raise ValueError(f"grain must be one of {GRAINS}, found {grain!r}")


async def _campaign_days_google(scope: QueryScope) -> list[CampaignDay]:
    """``campaign_day_google``: Google campaign days in the account's native dates."""
    dataset_id = scope.dataset_id("google")
    if dataset_id is None:
        return []
    fact = FactCampaignDaily
    stmt = (
        sa.select(
            fact.campaign_id,
            fact.date,
            fact.cost,
            fact.impressions,
            fact.clicks,
            fact.conversions,
            fact.conversions_value,
            fact.budget_lost_is,
            fact.impression_share,
        )
        .where(
            fact.dataset_id == dataset_id,
            fact.date.between(scope.window.start, scope.window.end),
        )
        .order_by(fact.campaign_id, fact.date)
    )
    result = await scope.session.execute(stmt)
    return [
        CampaignDay(
            campaign_id,
            DayFact(
                date=day,
                cost=_as_decimal(cost),
                impressions=_as_int(impressions),
                clicks=_as_int(clicks),
                conversions=_as_optional_decimal(conversions),
                conversions_value=_as_optional_decimal(value),
                budget_lost_is=_as_optional_decimal(lost_is),
                impression_share=_as_optional_decimal(share),
            ),
        )
        for campaign_id, day, cost, impressions, clicks, conversions, value, lost_is, share in (
            result.all()
        )
    ]


async def _campaign_days_linkedin(scope: QueryScope) -> list[CampaignDay]:
    """``campaign_day_linkedin``: creative days summed per campaign per UTC date."""
    dataset_id = scope.dataset_id("linkedin")
    if dataset_id is None:
        return []
    fact = FactCreativeDaily
    stmt = (
        sa.select(
            Creative.campaign_id,
            fact.date,
            sa.func.sum(fact.cost),
            _int_sum(fact.impressions),
            _int_sum(fact.clicks),
            sa.func.sum(fact.conversions),
            sa.func.sum(fact.conversions_value),
            _int_sum(fact.form_opens),
            _int_sum(fact.leads),
            _int_sum(fact.landing_page_clicks),
            _int_sum(fact.website_conversions),
        )
        .select_from(fact)
        .join(
            Creative,
            sa.and_(Creative.id == fact.creative_id, Creative.dataset_id == fact.dataset_id),
        )
        .where(
            fact.dataset_id == dataset_id,
            fact.date.between(scope.window.start, scope.window.end),
        )
        .group_by(Creative.campaign_id, fact.date)
        .order_by(Creative.campaign_id, fact.date)
    )
    result = await scope.session.execute(stmt)
    days: list[CampaignDay] = []
    for row in result.all():
        (
            campaign_id,
            day,
            cost,
            impressions,
            clicks,
            conversions,
            value,
            form_opens,
            leads,
            landing_page_clicks,
            website_conversions,
        ) = list(row)
        days.append(
            CampaignDay(
                campaign_id,
                DayFact(
                    date=day,
                    cost=_as_decimal(cost),
                    impressions=_as_int(impressions),
                    clicks=_as_int(clicks),
                    conversions=_as_optional_decimal(conversions),
                    conversions_value=_as_optional_decimal(value),
                    form_opens=_as_optional_int(form_opens),
                    leads=_as_optional_int(leads),
                    landing_page_clicks=_as_optional_int(landing_page_clicks),
                    website_conversions=_as_optional_int(website_conversions),
                ),
            )
        )
    return days


async def _creative_window(scope: QueryScope) -> list[CreativeTotals]:
    """``creative_window``: window totals per LinkedIn creative, with its delivery days."""
    dataset_id = scope.dataset_id("linkedin")
    if dataset_id is None:
        return []
    fact = FactCreativeDaily
    stmt = (
        sa.select(
            fact.creative_id,
            Creative.campaign_id,
            sa.func.sum(fact.cost),
            _int_sum(fact.impressions),
            _int_sum(fact.clicks),
            _int_sum(fact.form_opens),
            _int_sum(fact.leads),
            _int_sum(fact.landing_page_clicks),
            _int_sum(fact.website_conversions),
            sa.func.count().filter(fact.impressions > 0),
        )
        .select_from(fact)
        .join(
            Creative,
            sa.and_(Creative.id == fact.creative_id, Creative.dataset_id == fact.dataset_id),
        )
        .where(
            fact.dataset_id == dataset_id,
            fact.date.between(scope.window.start, scope.window.end),
        )
        .group_by(fact.creative_id, Creative.campaign_id)
        .order_by(fact.creative_id)
    )
    result = await scope.session.execute(stmt)
    totals: list[CreativeTotals] = []
    for row in result.all():
        (
            creative_id,
            campaign_id,
            cost,
            impressions,
            clicks,
            form_opens,
            leads,
            landing_page_clicks,
            website_conversions,
            delivery_days,
        ) = list(row)
        totals.append(
            CreativeTotals(
                creative_id=creative_id,
                campaign_id=campaign_id,
                cost=_as_decimal(cost),
                impressions=_as_int(impressions),
                clicks=_as_int(clicks),
                form_opens=_as_optional_int(form_opens),
                leads=_as_optional_int(leads),
                landing_page_clicks=_as_optional_int(landing_page_clicks),
                website_conversions=_as_optional_int(website_conversions),
                delivery_days=_as_int(delivery_days),
            )
        )
    return totals


async def _keyword_days(scope: QueryScope) -> list[KeywordDay]:
    """``keyword_day``: Google keyword days (Q-G5) in the account's native dates."""
    dataset_id = scope.dataset_id("google")
    if dataset_id is None:
        return []
    fact = FactKeywordDaily
    stmt = (
        sa.select(
            fact.keyword_id,
            fact.date,
            fact.cost,
            fact.clicks,
            fact.impressions,
            fact.conversions,
            fact.historical_quality_score,
            fact.average_cpc,
        )
        .where(
            fact.dataset_id == dataset_id,
            fact.date.between(scope.window.start, scope.window.end),
        )
        .order_by(fact.keyword_id, fact.date)
    )
    result = await scope.session.execute(stmt)
    return [
        KeywordDay(
            keyword_id=keyword_id,
            date=day,
            cost=_as_decimal(cost),
            clicks=_as_int(clicks),
            impressions=_as_int(impressions),
            conversions=_as_optional_decimal(conversions),
            historical_quality_score=_as_optional_int(quality_score),
            average_cpc=_as_optional_decimal(average_cpc),
        )
        for keyword_id, day, cost, clicks, impressions, conversions, quality_score, average_cpc in (
            result.all()
        )
    ]


async def _search_term_window(scope: QueryScope) -> list[SearchTermRow]:
    """``search_term_window``: window totals per (ad group, campaign, criterion, term).

    The status, matched keyword text and match type come from the latest date in the window
    (``array_agg(… ORDER BY date DESC)[1]``), so a term excluded mid-window reads as excluded.
    Conversions sum to 0 when every day is ``NULL``, because :class:`SearchTermRow` carries
    them as a number; Q-G6 reports ``metrics.conversions`` on every row it returns.

    Rows are ordered by ad group, then rows with a matched criterion before those without
    (the stored ``''``, read back as ``None``), then criterion and term in code-point order
    (:data:`_CODE_POINT`): the order ``context_from_world`` builds, on any database locale.
    """
    dataset_id = scope.dataset_id("google")
    if dataset_id is None:
        return []
    fact = FactSearchTermDaily
    latest_first = (fact.date.desc(), fact.id.desc())
    # The column is NOT NULL and stores '' for "no keyword segment"; a NULL sorts with it.
    no_criterion = sa.func.coalesce(fact.matched_criterion_id, "") == ""
    stmt = (
        sa.select(
            fact.ad_group_id,
            fact.campaign_id,
            fact.matched_criterion_id,
            fact.search_term,
            sa.func.array_agg(aggregate_order_by(fact.term_status, *latest_first))[1],
            sa.func.array_agg(aggregate_order_by(fact.matched_keyword_text, *latest_first))[1],
            sa.func.array_agg(aggregate_order_by(fact.matched_match_type, *latest_first))[1],
            sa.func.sum(fact.cost),
            _int_sum(fact.clicks),
            _int_sum(fact.impressions),
            sa.func.sum(fact.conversions),
            sa.func.sum(fact.conversions_value),
        )
        .where(
            fact.dataset_id == dataset_id,
            fact.date.between(scope.window.start, scope.window.end),
        )
        .group_by(fact.ad_group_id, fact.campaign_id, fact.matched_criterion_id, fact.search_term)
        .order_by(
            fact.ad_group_id,
            no_criterion,
            fact.matched_criterion_id.collate(_CODE_POINT),
            fact.search_term.collate(_CODE_POINT),
        )
    )
    result = await scope.session.execute(stmt)
    rows: list[SearchTermRow] = []
    for row in result.all():
        (
            ad_group_id,
            campaign_id,
            criterion,
            term,
            status,
            keyword_text,
            match_type,
            cost,
            clicks,
            impressions,
            conversions,
            value,
        ) = list(row)
        rows.append(
            SearchTermRow(
                ad_group_id=ad_group_id,
                campaign_id=campaign_id,
                search_term=term,
                term_status=str(status),
                matched_criterion_id=criterion or None,
                matched_keyword_text=None if keyword_text is None else str(keyword_text),
                matched_match_type=None if match_type is None else str(match_type),
                cost=_as_decimal(cost),
                clicks=_as_int(clicks),
                impressions=_as_int(impressions),
                conversions=_as_decimal(conversions),
                conversions_value=_as_optional_decimal(value),
            )
        )
    return rows


async def _google_rate_baseline(scope: QueryScope) -> list[RateBaseline]:
    """``google_rate_baseline``: K1's account conversions and clicks over ``k1.baseline_days``.

    The span ends at ``window.end`` and runs ``k1.baseline_days`` native Google days back.
    Returns an empty list when the span holds no ``fact_campaign_daily`` row at all, so the
    context's ``k1_baseline`` is ``None`` ("unavailable") rather than a zero rate.
    """
    dataset_id = scope.dataset_id("google")
    if dataset_id is None:
        return []
    # A validated configuration holds k1.baseline_days >= 1 (schema kind "count").
    days = scope.thresholds.int("k1.baseline_days")
    end = scope.window.end
    start = end - timedelta(days=days - 1)
    fact = FactCampaignDaily
    stmt = sa.select(sa.func.sum(fact.conversions), _int_sum(fact.clicks), sa.func.count()).where(
        fact.dataset_id == dataset_id, fact.date.between(start, end)
    )
    conversions, clicks, row_count = (await scope.session.execute(stmt)).one()
    if not row_count:
        return []
    return [
        RateBaseline(
            conversions=_as_decimal(conversions), clicks=_as_int(clicks), start=start, end=end
        )
    ]


async def _segment_window(scope: QueryScope) -> list[SegmentRow]:
    """``segment_window``: LinkedIn segment rows fetched for exactly this run's window (C3a).

    Ordered by campaign, then facet and value in code-point order (:data:`_CODE_POINT`).
    """
    dataset_id = scope.dataset_id("linkedin")
    if dataset_id is None:
        return []
    seg = FactSegmentWindow
    stmt = (
        sa.select(
            seg.campaign_id,
            seg.facet,
            seg.value,
            seg.window_start,
            seg.window_end,
            seg.impressions,
            seg.clicks,
            seg.cost,
            seg.leads,
        )
        .where(
            seg.dataset_id == dataset_id,
            seg.window_start == scope.window.start,
            seg.window_end == scope.window.end,
        )
        .order_by(seg.campaign_id, seg.facet.collate(_CODE_POINT), seg.value.collate(_CODE_POINT))
    )
    result = await scope.session.execute(stmt)
    rows: list[SegmentRow] = []
    for row in result.all():
        (campaign_id, facet, value, window_start, window_end, impressions, clicks, cost, leads) = (
            row
        )
        rows.append(
            SegmentRow(
                campaign_id=campaign_id,
                facet=facet,
                value=value,
                window_start=window_start,
                window_end=window_end,
                impressions=_as_optional_int(impressions),
                clicks=_as_optional_int(clicks),
                cost=_as_optional_decimal(cost),
                leads=_as_optional_int(leads),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# SQL half: the context loader (AAP 0.5.1, 0.3.3 decision 8)
# ---------------------------------------------------------------------------


def _dataset_ref(row: SourceDataset) -> DatasetRef:
    """Return a ``source_dataset`` row as a :class:`DatasetRef`."""
    return DatasetRef(
        dataset_id=row.id,
        source=_choice(row.source, _SOURCES_ALL, "source_dataset.source"),
        mode=_choice(row.mode, _DATASET_MODES, "source_dataset.mode"),
        role=_choice(row.role, _DATASET_ROLES, "source_dataset.role"),
    )


def _bindings(row: SourceDataset, now: datetime) -> CrmBindings:
    """Return a HubSpot dataset's verified CRM bindings, or all-unavailable bindings.

    A dataset whose bindings were never verified, or whose stored document no longer decodes,
    reports MQL, QL and the Google link as unavailable with the reason, never as 0
    (AAP 0.5.4.7). An undecodable document is logged, because it is a stored-state defect.
    """
    if row.crm_bindings is None:
        return CrmBindings.unavailable("CRM stage bindings not verified", at=now)
    try:
        return CrmBindings.from_json(row.crm_bindings)
    except ConfigError as exc:
        _log.warning(
            "analysis_crm_bindings_unreadable", dataset_id=row.id, key=exc.key, error=str(exc)
        )
        return CrmBindings.unavailable("CRM stage bindings unreadable", at=now)


async def _crm_datasets(
    session: AsyncSession, dataset_ids: Collection[int]
) -> dict[int, SourceDataset]:
    """Return the ``source_dataset`` rows of the CRM datasets a context reads."""
    if not dataset_ids:
        return {}
    result = await session.execute(
        sa.select(SourceDataset).where(SourceDataset.id.in_(sorted(dataset_ids)))
    )
    return {row.id: row for row in result.scalars()}


async def _load_freshness(
    session: AsyncSession, dataset_ids: Collection[int]
) -> dict[tuple[int, str], Freshness]:
    """Return the ``dataset_freshness`` rows of every dataset the context reads."""
    if not dataset_ids:
        return {}
    row = DatasetFreshness
    result = await session.execute(
        sa.select(
            row.dataset_id,
            row.dataset,
            row.last_attempt_at,
            row.last_success_at,
            row.covered_from,
            row.covered_through,
            row.completeness,
            row.reason,
        ).where(row.dataset_id.in_(sorted(dataset_ids)))
    )
    freshness: dict[tuple[int, str], Freshness] = {}
    for (
        dataset_id,
        name,
        attempt,
        success,
        covered_from,
        covered_through,
        state,
        reason,
    ) in result.all():
        freshness[(dataset_id, name)] = Freshness(
            dataset_id=dataset_id,
            dataset=name,
            last_attempt_at=attempt,
            last_success_at=success,
            covered_from=covered_from,
            covered_through=covered_through,
            completeness=_choice(state, _COMPLETENESS, "dataset_freshness.completeness"),
            reason=reason,
        )
    return freshness


@dataclass(slots=True)
class _Entities:
    """The entity maps of the active Google and LinkedIn datasets, filled by the loader."""

    accounts: dict[int, AccountInfo]
    campaign_groups: dict[int, CampaignGroupInfo]
    campaigns: dict[int, CampaignInfo]
    budgets: dict[int, BudgetInfo]
    ad_groups: dict[int, AdGroupInfo]
    keywords: dict[int, KeywordInfo]
    creatives: dict[int, CreativeInfo]
    conversion_actions: dict[int, ConversionActionInfo]
    budgets_without_currency: int = 0


async def _load_entities(scope: QueryScope) -> _Entities:
    """Load accounts, campaign groups, campaigns, budgets, ad groups, keywords and creatives.

    Every query filters to the active Google and LinkedIn datasets. A campaign carries its
    account's currency and :func:`objective_group`; a keyword carries its ad group's campaign.
    A Google budget takes the currency of the account of a campaign drawing from it, else of
    the dataset's first Google account; a budget with neither is not loaded (counted in the
    load log), because amounts never exist without a currency.
    """
    entities = _Entities({}, {}, {}, {}, {}, {}, {}, {})
    session = scope.session
    ad_ids = scope.ad_dataset_ids()
    if not ad_ids:
        return entities

    acct = AdAccount
    result = await session.execute(
        sa.select(acct.id, acct.source, acct.external_id, acct.name, acct.currency, acct.timezone)
        .where(acct.dataset_id.in_(ad_ids))
        .order_by(acct.id)
    )
    for account_id, source, external_id, account_name, currency, timezone in result.all():
        entities.accounts[account_id] = AccountInfo(
            id=account_id,
            source=_choice(source, _AD_SOURCES, "ad_account.source"),
            external_id=external_id,
            name=account_name or external_id,
            currency=currency,
            timezone=timezone,
        )

    linkedin = scope.dataset_id("linkedin")
    if linkedin is not None:
        group = CampaignGroup
        result_groups = await session.execute(
            sa.select(
                group.id,
                group.account_id,
                group.external_id,
                group.name,
                group.status,
                group.daily_budget,
                group.total_budget,
            )
            .where(group.dataset_id == linkedin)
            .order_by(group.id)
        )
        for group_id, account_id, external_id, name, status, daily, total in result_groups.all():
            entities.campaign_groups[group_id] = CampaignGroupInfo(
                id=group_id,
                account_id=account_id,
                external_id=external_id,
                name=name,
                status=status,
                daily_budget=daily,
                total_budget=total,
            )

    camp = Campaign
    result_campaigns = await session.execute(
        sa.select(
            camp.id,
            camp.source,
            camp.account_id,
            camp.external_id,
            camp.name,
            camp.status,
            camp.raw_status,
            camp.objective_or_channel,
            camp.campaign_group_id,
            camp.budget_id,
            camp.daily_budget,
            camp.total_budget,
            camp.budget_period,
        )
        .where(camp.dataset_id.in_(ad_ids))
        .order_by(camp.id)
    )
    for row in result_campaigns.all():
        (
            campaign_id,
            source,
            account_id,
            external_id,
            name,
            status,
            raw_status,
            objective,
            group_id,
            budget_id,
            daily,
            total,
            period,
        ) = list(row)
        ad_source = _choice(source, _AD_SOURCES, "campaign.source")
        entities.campaigns[campaign_id] = CampaignInfo(
            id=campaign_id,
            source=ad_source,
            account_id=account_id,
            external_id=external_id,
            name=name,
            status=_choice(status, _CAMPAIGN_STATUSES, "campaign.status"),
            raw_status=raw_status,
            objective_or_channel=objective,
            objective_group=objective_group(ad_source, objective),
            campaign_group_id=group_id,
            budget_id=budget_id,
            daily_budget=daily,
            total_budget=total,
            budget_period=_optional_choice(period, _BUDGET_PERIODS, "campaign.budget_period"),
            currency=entities.accounts[account_id].currency,
        )

    google = scope.dataset_id("google")
    if google is not None:
        await _load_google_entities(session, google, entities)
    if linkedin is not None:
        await _load_linkedin_entities(session, linkedin, entities)
    return entities


async def _load_google_entities(session: AsyncSession, dataset_id: int, out: _Entities) -> None:
    """Load the active Google dataset's budgets, ad groups, keywords and conversion actions."""
    budget_currency: dict[int, str] = {}
    for campaign in out.campaigns.values():
        if campaign.source == "google" and campaign.budget_id is not None:
            budget_currency.setdefault(campaign.budget_id, campaign.currency)
    fallback_currency = next(
        (acct.currency for acct in out.accounts.values() if acct.source == "google"), None
    )

    bud = Budget
    result = await session.execute(
        sa.select(
            bud.id,
            bud.external_id,
            bud.amount,
            bud.amount_micros,
            bud.period,
            bud.explicitly_shared,
        )
        .where(bud.dataset_id == dataset_id)
        .order_by(bud.id)
    )
    for budget_id, external_id, amount, micros, period, shared in result.all():
        currency = budget_currency.get(budget_id, fallback_currency)
        if currency is None:
            out.budgets_without_currency += 1
            continue
        out.budgets[budget_id] = BudgetInfo(
            id=budget_id,
            external_id=external_id,
            name="",
            amount=amount,
            amount_micros=micros,
            period=_choice(period, _BUDGET_PERIODS, "budget.period"),
            explicitly_shared=bool(shared),
            currency=currency,
        )

    group = AdGroup
    result_groups = await session.execute(
        sa.select(group.id, group.campaign_id, group.external_id, group.name, group.status)
        .where(group.dataset_id == dataset_id)
        .order_by(group.id)
    )
    for group_id, campaign_id, external_id, name, status in result_groups.all():
        out.ad_groups[group_id] = AdGroupInfo(group_id, campaign_id, external_id, name, status)

    kw = Keyword
    result_keywords = await session.execute(
        sa.select(
            kw.id,
            kw.ad_group_id,
            AdGroup.campaign_id,
            kw.criterion_id,
            kw.text,
            kw.match_type,
            kw.status,
            kw.quality_score,
        )
        .select_from(kw)
        .join(AdGroup, sa.and_(AdGroup.id == kw.ad_group_id, AdGroup.dataset_id == kw.dataset_id))
        .where(kw.dataset_id == dataset_id)
        .order_by(kw.id)
    )
    for (
        keyword_id,
        ad_group_id,
        campaign_id,
        criterion,
        text,
        match_type,
        status,
        qs,
    ) in result_keywords.all():
        out.keywords[keyword_id] = KeywordInfo(
            id=keyword_id,
            ad_group_id=ad_group_id,
            campaign_id=campaign_id,
            criterion_id=criterion,
            text=text,
            match_type=match_type,
            status=status,
            quality_score=qs,
        )

    action = ConversionAction
    result_actions = await session.execute(
        sa.select(
            action.id,
            action.external_id,
            action.name,
            action.status,
            action.include_in_conversions_metric,
            action.counting_type,
            action.attribution_model,
        )
        .where(action.dataset_id == dataset_id)
        .order_by(action.id)
    )
    for action_id, external_id, name, status, included, counting, model in result_actions.all():
        out.conversion_actions[action_id] = ConversionActionInfo(
            id=action_id,
            external_id=external_id,
            name=name,
            status=status,
            include_in_conversions_metric=bool(included),
            counting_type=counting,
            attribution_model=model,
        )


async def _load_linkedin_entities(session: AsyncSession, dataset_id: int, out: _Entities) -> None:
    """Load the active LinkedIn dataset's creatives."""
    cre = Creative
    result = await session.execute(
        sa.select(
            cre.id,
            cre.campaign_id,
            cre.urn,
            cre.intended_status,
            cre.headline,
            cre.intro_text,
            cre.cta_label,
            cre.lead_form_urn,
        )
        .where(cre.dataset_id == dataset_id)
        .order_by(cre.id)
    )
    for creative_id, campaign_id, urn, status, headline, intro, cta, form in result.all():
        out.creatives[creative_id] = CreativeInfo(
            id=creative_id,
            campaign_id=campaign_id,
            urn=urn,
            intended_status=status,
            headline=headline,
            intro_text=intro,
            cta_label=cta,
            lead_form_urn=form,
        )


def _observation(
    captured_at: datetime,
    status: str | None,
    budget_daily: Decimal | None,
    budget_total: Decimal | None,
    micros: int | None,
    budget_id: int | None,
    quality_score: int | None,
    payload: Mapping[str, Any] | None,
) -> Observation:
    """Build an :class:`Observation` from snapshot columns; the payload is made read-only."""
    return Observation(
        captured_at=captured_at,
        status=status,
        budget_daily=budget_daily,
        budget_total=budget_total,
        budget_amount_micros=micros,
        budget_id=budget_id,
        quality_score=quality_score,
        payload=MappingProxyType(dict(payload or {})),
    )


def _snapshot_columns() -> tuple[Any, ...]:
    """The ``entity_snapshot`` columns an :class:`Observation` is built from, keyed first."""
    snap = EntitySnapshot
    return (
        snap.entity_type,
        snap.entity_id,
        snap.captured_at,
        snap.status,
        snap.budget_daily,
        snap.budget_total,
        snap.budget_amount_micros,
        snap.budget_id,
        snap.quality_score,
        snap.payload,
    )


def _observation_bounds(window: AnalysisWindow, max_gap_hours: int) -> tuple[datetime, datetime]:
    """Return the instant range of observations budget-day classification can use.

    ``[utc(window.start) - margin, utc(window.end + 1 day) + margin)`` with a margin of at
    least two days. The margin also covers the largest UTC offset of any native-day zone plus
    ``budget.max_observation_gap_hours``: an observation farther from a window day than that
    can never be part of a whole-day-known span, so leaving it out changes no classification.
    """
    margin = max(timedelta(days=2), _MAX_UTC_OFFSET + timedelta(hours=max_gap_hours))
    lower, upper = window.utc_bounds()
    return lower - margin, upper + margin


async def _load_observations(scope: QueryScope) -> dict[tuple[str, int], list[Observation]]:
    """Load campaign, budget and conversion-action observations bracketing the window.

    Ordered ascending by ``captured_at`` (then id) within each ``(entity_type, entity_id)``,
    which serves the ``ix_entity_snapshot_entity_captured`` index.
    """
    ad_ids = scope.ad_dataset_ids()
    if not ad_ids:
        return {}
    gap = scope.thresholds.int("budget.max_observation_gap_hours")
    lower, upper = _observation_bounds(scope.window, gap)
    snap = EntitySnapshot
    stmt = (
        sa.select(*_snapshot_columns())
        .where(
            snap.dataset_id.in_(ad_ids),
            snap.entity_type.in_(_OBSERVED_TYPES),
            snap.captured_at >= lower,
            snap.captured_at < upper,
        )
        .order_by(snap.entity_type, snap.entity_id, snap.captured_at, snap.id)
    )
    grouped: dict[tuple[str, int], list[Observation]] = defaultdict(list)
    for entity_type, entity_id, *columns in (await scope.session.execute(stmt)).all():
        grouped[(entity_type, entity_id)].append(_observation(*columns))
    return grouped


async def _load_latest_observations(scope: QueryScope) -> dict[tuple[str, int], Observation]:
    """Load each campaign's and budget's newest snapshot captured at or before ``scope.now``.

    The current state budget proposals are sized from (AAP 0.5.4.5 B1: "the latest effective
    daily budget"), read independently of the window: a custom run over a window that ended
    long ago still finds today's amount, because active snapshots are never purged
    (AAP 0.3.2). Each campaign and budget row of the active Google and LinkedIn datasets
    drives one ``LATERAL`` probe ``ORDER BY captured_at DESC, id DESC LIMIT 1``, a bounded
    scan of ``ix_entity_snapshot_entity_captured`` however long the history grows. The probe
    also requires the entity's own ``dataset_id``, so a snapshot of a retired dataset that
    reuses an internal id can never stand in, and a snapshot after the run time is never read.
    An entity without such a snapshot is absent.
    """
    ad_ids = scope.ad_dataset_ids()
    if not ad_ids:
        return {}
    snap = EntitySnapshot
    latest: dict[tuple[str, int], Observation] = {}
    for entity_type, entity in (("campaign", Campaign), ("budget", Budget)):
        newest = (
            sa.select(*_snapshot_columns())
            .where(
                snap.entity_type == entity_type,
                snap.entity_id == entity.id,
                snap.dataset_id == entity.dataset_id,
                snap.captured_at <= scope.now,
            )
            .order_by(snap.captured_at.desc(), snap.id.desc())
            .limit(1)
            .lateral(f"newest_{entity_type}_snapshot")
        )
        stmt = (
            sa.select(*newest.c)
            .select_from(entity)
            .join(newest, sa.true())
            .where(entity.dataset_id.in_(ad_ids))
            .order_by(entity.id)
        )
        for _entity_type, entity_id, *columns in (await scope.session.execute(stmt)).all():
            latest[(entity_type, entity_id)] = _observation(*columns)
    return latest


def _window_boundary(window: AnalysisWindow, source: Literal["google", "linkedin"]) -> str | None:
    """The day boundary ``source`` uses in ``window``, or ``None`` when it is not a day source."""
    return next((item.boundary for item in window.source_days if item.source == source), None)


def _google_window_instants(scope: QueryScope, timezone: str | None) -> tuple[datetime, datetime]:
    """Return the window's instant range in Google's native day: ``[start 00:00, end + 1)``."""
    zone = _window_boundary(scope.window, "google") or timezone or UTC_BOUNDARY
    lower, _ = AnalysisWindow.day_bounds(scope.window.start, zone)
    _, upper = AnalysisWindow.day_bounds(scope.window.end, zone)
    return lower, upper


async def _load_keyword_observations(
    scope: QueryScope, timezone: str | None
) -> dict[tuple[str, int], list[Observation]]:
    """Load, per keyword, its first observation at/after window start and last at/before end.

    K3's snapshot fallback compares exactly these two Q-G7 quality-score observations
    (AAP 0.5.4.3), and no other rule reads keyword snapshots. Keywords are snapshotted every
    entity sync, so a full window history is thousands of rows per keyword; reading only the
    two that matter (``DISTINCT ON`` over ``ix_entity_snapshot_entity_captured``) keeps a
    180-day custom run inside the AAP 0.3.2 budget of 30 s.
    """
    google = scope.dataset_id("google")
    if google is None:
        return {}
    lower, upper = _google_window_instants(scope, timezone)
    snap = EntitySnapshot
    where = (
        snap.dataset_id == google,
        snap.entity_type == "keyword",
        snap.captured_at >= lower,
        snap.captured_at <= upper,
    )
    first = (
        sa.select(*_snapshot_columns())
        .where(*where)
        .order_by(snap.entity_id, snap.captured_at.asc(), snap.id.asc())
        .ext(distinct_on(snap.entity_id))
    )
    last = (
        sa.select(*_snapshot_columns())
        .where(*where)
        .order_by(snap.entity_id, snap.captured_at.desc(), snap.id.desc())
        .ext(distinct_on(snap.entity_id))
    )
    picked: dict[int, dict[datetime, Observation]] = defaultdict(dict)
    for stmt in (first, last):
        for _entity_type, entity_id, *columns in (await scope.session.execute(stmt)).all():
            observation = _observation(*columns)
            picked[entity_id][observation.captured_at] = observation
    return {
        ("keyword", entity_id): [by_time[instant] for instant in sorted(by_time)]
        for entity_id, by_time in picked.items()
    }


@dataclass(slots=True)
class _Leads:
    """The window's LinkedIn leads and the internal ids of their matched contacts."""

    leads: list[LeadInfo]
    matched_contact_ids: set[int]


async def _load_leads(scope: QueryScope) -> _Leads:
    """Load the LinkedIn leads submitted in ``window.utc_bounds()`` with their match state.

    The match is read from the effective CRM dataset only (``lead_match.crm_dataset_id``); a
    lead with no row there keeps ``match_status`` ``None`` and counts as unresolved
    (AAP 0.5.4.1). Test leads are never stored, so none can appear.
    """
    linkedin = scope.dataset_id("linkedin")
    if linkedin is None:
        return _Leads([], set())
    lower, upper = scope.window.utc_bounds()
    crm_id = scope.linkedin_crm_id
    match_join = (
        sa.and_(LeadMatch.lead_id == Lead.id, LeadMatch.crm_dataset_id == crm_id)
        if crm_id is not None
        else sa.false()
    )
    stmt = (
        sa.select(
            Lead.id,
            Lead.external_id,
            Lead.campaign_id,
            Lead.creative_id,
            Creative.urn,
            Lead.lead_form_urn,
            Lead.lead_form_version,
            Lead.submitted_at,
            LeadMatch.status,
            LeadMatch.contact_id,
            CrmContact.hs_id,
            LeadMatch.contact_read_status,
            LeadMatch.reason,
        )
        .select_from(Lead)
        .outerjoin(LeadMatch, match_join)
        .outerjoin(
            CrmContact,
            sa.and_(
                CrmContact.id == LeadMatch.contact_id,
                CrmContact.dataset_id == LeadMatch.crm_dataset_id,
            ),
        )
        .outerjoin(
            Creative, sa.and_(Creative.id == Lead.creative_id, Creative.dataset_id == linkedin)
        )
        .where(
            Lead.dataset_id == linkedin,
            Lead.submitted_at >= lower,
            Lead.submitted_at < upper,
        )
        .order_by(Lead.submitted_at, Lead.id)
    )
    loaded = _Leads([], set())
    for row in (await scope.session.execute(stmt)).all():
        (
            lead_id,
            external_id,
            campaign_id,
            creative_id,
            creative_urn,
            form_urn,
            version,
            submitted_at,
            status,
            contact_id,
            hs_id,
            read_status,
            reason,
        ) = list(row)
        match_status = _optional_choice(status, _MATCH_STATUSES, "lead_match.status")
        if match_status == "matched" and contact_id is not None:
            loaded.matched_contact_ids.add(contact_id)
        loaded.leads.append(
            LeadInfo(
                id=lead_id,
                external_id=external_id,
                campaign_id=campaign_id,
                creative_id=creative_id,
                creative_urn=creative_urn,
                lead_form_urn=form_urn,
                lead_form_version=version,
                submitted_at=submitted_at,
                match_status=match_status,
                contact_hs_id=hs_id,
                contact_read_status=_optional_choice(
                    read_status, _CONTACT_READ, "lead_match.contact_read_status"
                ),
                match_reason=reason,
            )
        )
    return loaded


def _any_text(column: Any, values: Collection[str]) -> sa.ColumnElement[bool]:
    """``column = ANY(:values)``: one array parameter, however many ids (no IN-list limit)."""
    clause: sa.ColumnElement[bool] = column == sa.any_(
        sa.literal(sorted(values), sa.ARRAY(sa.Text()))
    )
    return clause


def _any_int(column: Any, values: Collection[int]) -> sa.ColumnElement[bool]:
    """``column = ANY(:values)`` for internal ids."""
    clause: sa.ColumnElement[bool] = column == sa.any_(
        sa.literal(sorted(values), sa.ARRAY(sa.BigInteger()))
    )
    return clause


def _contact_info(
    hs_id: str,
    stage: str | None,
    mql_at: datetime | None,
    ql_at: datetime | None,
    campaign_ref: str | None,
    touch_at: datetime | None,
) -> ContactInfo:
    """Build a :class:`ContactInfo` from ``crm_contact`` columns."""
    return ContactInfo(
        hs_id=hs_id,
        lifecycle_stage=stage,
        mql_at=mql_at,
        ql_at=ql_at,
        google_campaign_ref=campaign_ref,
        google_touch_at=touch_at,
    )


def _contact_columns() -> tuple[Any, ...]:
    """The ``crm_contact`` columns a :class:`ContactInfo` is built from."""
    ct = CrmContact
    return (
        ct.hs_id,
        ct.lifecycle_stage,
        ct.mql_at,
        ct.ql_at,
        ct.google_campaign_ref,
        ct.google_touch_at,
    )


async def _load_crm(
    scope: QueryScope,
    row: SourceDataset,
    *,
    contact_ids: Collection[int],
    google_contacts: bool,
    freshness: Mapping[tuple[int, str], Freshness],
    google_timezone: str | None,
) -> CrmData:
    """Load everything a context reads from one HubSpot dataset (AAP 0.3.3, 0.5.4.7).

    * contacts: the matched leads' contacts (``contact_ids``) and, when ``google_contacts``
      and the dataset's bindings verify the Google link, the Google-touch contacts:
      ``google_campaign_ref`` set and ``google_touch_at`` within the window's Google-native
      instants (:func:`_google_window_instants`: ``[start 00:00, end + 1 day 00:00)`` at the
      window's Google boundary, else ``google_timezone``, else UTC; AAP 0.5.4.1 native days).
      Bindings never verified, undecodable or with the link unavailable discover none, so
      Google refs stored under a link that is no longer verified never enter the load;
    * contact-to-meeting and contact-to-deal associations and fetch outcomes of those
      contacts, and the associated meetings and deals;
    * window deals (``created_at`` from window start, ``window.utc_bounds()[0]``, to ``now``),
      their deal-to-contact associations and fetch outcomes;
    * ``deal_search_complete``: the dataset's ``hubspot.deal_search`` row is ``complete`` and
      covers the window's dates.

    An associated id with no stored meeting or deal row is simply absent from ``meetings`` or
    ``deals``; its fetch row's target-read status already marks it unknown, never absent.

    Order, as ``context_from_world`` builds it: every mapping is keyed in sorted key order,
    association targets are in code-point order, window deals are ordered by creation, then
    id in code-point order, and Google-touch contacts by touch, then id in code-point order
    (:data:`_CODE_POINT`).
    """
    session = scope.session
    dataset_id = row.id
    bindings = _bindings(row, scope.now)
    lower = scope.window.utc_bounds()[0]
    contacts: dict[str, ContactInfo] = {}
    ct = CrmContact
    if contact_ids:
        result = await session.execute(
            sa.select(*_contact_columns()).where(
                ct.dataset_id == dataset_id, _any_int(ct.id, contact_ids)
            )
        )
        for columns in result.all():
            info = _contact_info(*columns)
            contacts[info.hs_id] = info
    discovered: list[ContactInfo] = []
    if google_contacts and bindings.google.available:
        touch_lower, touch_upper = _google_window_instants(scope, google_timezone)
        result = await session.execute(
            sa.select(*_contact_columns())
            .where(
                ct.dataset_id == dataset_id,
                ct.google_campaign_ref.is_not(None),
                ct.google_touch_at >= touch_lower,
                ct.google_touch_at < touch_upper,
            )
            .order_by(ct.google_touch_at, ct.hs_id.collate(_CODE_POINT))
        )
        for columns in result.all():
            info = _contact_info(*columns)
            discovered.append(info)
            contacts.setdefault(info.hs_id, info)

    deals: dict[str, DealInfo] = {}
    deal = CrmDeal
    deal_columns = (deal.hs_id, deal.created_at, deal.stage, deal.pipeline, deal.amount)
    result_deals = await session.execute(
        sa.select(*deal_columns)
        .where(
            deal.dataset_id == dataset_id,
            deal.created_at >= lower,
            deal.created_at <= scope.now,
        )
        .order_by(deal.created_at, deal.hs_id.collate(_CODE_POINT))
    )
    window_deal_ids: list[str] = []
    for hs_id, created_at, stage, pipeline, amount in result_deals.all():
        deals[hs_id] = DealInfo(hs_id, created_at, stage, pipeline, amount)
        window_deal_ids.append(hs_id)

    associations: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    fetches: dict[tuple[str, str, str], AssociationFetchInfo] = {}
    scopes: list[tuple[str, Collection[str], tuple[str, ...]]] = [
        ("contact", contacts.keys(), _CRM_LINKED_TYPES),
        ("deal", window_deal_ids, ("contact",)),
    ]
    assoc = CrmAssociation
    fetch = CrmAssociationFetch
    for from_type, from_ids, to_types in scopes:
        if not from_ids:
            continue
        result_assoc = await session.execute(
            sa.select(assoc.from_type, assoc.from_id, assoc.to_type, assoc.to_id)
            .where(
                assoc.dataset_id == dataset_id,
                assoc.from_type == from_type,
                _any_text(assoc.from_id, from_ids),
                assoc.to_type.in_(to_types),
            )
            .order_by(
                assoc.from_id.collate(_CODE_POINT),
                assoc.to_type.collate(_CODE_POINT),
                assoc.to_id.collate(_CODE_POINT),
            )
        )
        for a_from_type, a_from_id, a_to_type, a_to_id in result_assoc.all():
            associations[(a_from_type, a_from_id, a_to_type)].append(a_to_id)
        result_fetch = await session.execute(
            sa.select(
                fetch.object_type,
                fetch.object_id,
                fetch.to_type,
                fetch.status,
                fetch.target_read_status,
                fetch.missing_target_ids,
            ).where(
                fetch.dataset_id == dataset_id,
                fetch.object_type == from_type,
                _any_text(fetch.object_id, from_ids),
                fetch.to_type.in_(to_types),
            )
        )
        for object_type, object_id, to_type, status, target_status, missing in result_fetch.all():
            fetches[(object_type, object_id, to_type)] = AssociationFetchInfo(
                object_type=object_type,
                object_id=object_id,
                to_type=to_type,
                status=_choice(status, _ASSOCIATION_STATUSES, "crm_association_fetch.status"),
                target_read_status=_optional_choice(
                    target_status, _TARGET_READ, "crm_association_fetch.target_read_status"
                ),
                missing_target_ids=tuple(missing or ()),
            )

    meeting_ids = {
        to_id
        for (kind, _, to_type), ids in associations.items()
        if kind == "contact"
        for to_id in ids
        if to_type == "meeting"
    }
    deal_ids = {
        to_id
        for (kind, _, to_type), ids in associations.items()
        if kind == "contact"
        for to_id in ids
        if to_type == "deal"
    } - deals.keys()
    meetings: dict[str, MeetingInfo] = {}
    if meeting_ids:
        mt = CrmMeeting
        result_meetings = await session.execute(
            sa.select(
                mt.hs_id,
                mt.created_at,
                mt.start_at,
                mt.meeting_timestamp,
                mt.outcome,
                mt.meeting_source,
            ).where(mt.dataset_id == dataset_id, _any_text(mt.hs_id, meeting_ids))
        )
        for hs_id, created_at, start_at, stamp, outcome_value, source in result_meetings.all():
            meetings[hs_id] = MeetingInfo(hs_id, created_at, start_at, stamp, outcome_value, source)
    if deal_ids:
        result_linked = await session.execute(
            sa.select(*deal_columns).where(
                deal.dataset_id == dataset_id, _any_text(deal.hs_id, deal_ids)
            )
        )
        for hs_id, created_at, stage, pipeline, amount in result_linked.all():
            deals[hs_id] = DealInfo(hs_id, created_at, stage, pipeline, amount)

    search = freshness.get((dataset_id, "hubspot.deal_search"))
    deal_search_complete = (
        search is not None
        and search.is_complete
        and search.covered_from is not None
        and search.covered_through is not None
        and search.covered_from <= scope.window.start
        and search.covered_through >= scope.window.end
    )
    return CrmData(
        dataset=_dataset_ref(row),
        bindings=bindings,
        contacts=MappingProxyType(dict(sorted(contacts.items()))),
        meetings=MappingProxyType(dict(sorted(meetings.items()))),
        deals=MappingProxyType(dict(sorted(deals.items()))),
        associations=MappingProxyType(
            {key: tuple(ids) for key, ids in sorted(associations.items())}
        ),
        fetches=MappingProxyType(dict(sorted(fetches.items()))),
        window_deal_ids=tuple(window_deal_ids),
        deal_search_complete=deal_search_complete,
        google_contacts=tuple(discovered),
    )


async def _load_decisions(
    session: AsyncSession, ad_ids: Sequence[int]
) -> dict[SubjectKey, DecisionInfo]:
    """Return the latest decision per subject of the active Google and LinkedIn datasets.

    ``DISTINCT ON (subject_type, subject_id) … ORDER BY decided_at DESC, id DESC``: the newest
    row is the "last decided" state (AAP 0.5.2); the id breaks a tie at one instant.
    """
    if not ad_ids:
        return {}
    dec = Decision
    stmt = (
        sa.select(
            dec.subject_type,
            dec.subject_id,
            dec.shape,
            dec.status,
            dec.daily_budget,
            dec.total_budget,
            dec.budget_id,
            dec.tombstone,
            dec.decided_at,
            dec.origin,
        )
        .where(dec.dataset_id.in_(ad_ids))
        .order_by(dec.subject_type, dec.subject_id, dec.decided_at.desc(), dec.id.desc())
        .ext(distinct_on(dec.subject_type, dec.subject_id))
    )
    decisions: dict[SubjectKey, DecisionInfo] = {}
    for row in (await session.execute(stmt)).all():
        (
            subject_type,
            subject_id,
            shape,
            status,
            daily,
            total,
            budget_id,
            tombstone,
            decided_at,
            origin,
        ) = list(row)
        info = DecisionInfo(
            subject_type=_choice(subject_type, _SUBJECT_TYPES, "decision.subject_type"),
            subject_id=subject_id,
            shape=_choice(shape, _DECISION_SHAPES, "decision.shape"),
            status=status,
            daily_budget=daily,
            total_budget=total,
            budget_id=budget_id,
            tombstone=bool(tombstone),
            decided_at=decided_at,
            origin=_choice(origin, _DECISION_ORIGINS, "decision.origin"),
        )
        decisions[info.key] = info
    return decisions


def _inferred_shape(entities: _Entities, key: SubjectKey) -> DecisionShape:
    """The decision shape of a subject that has no decision yet (an ``untracked`` one).

    A budget subject is ``budget``; a campaign drawing from an explicitly shared budget is a
    ``shared_member``; every other campaign is ``own_budget`` (AAP 0.5.2).
    """
    subject_type, subject_id = key
    if subject_type == "budget":
        return "budget"
    campaign = entities.campaigns.get(subject_id)
    if campaign is not None and campaign.budget_id is not None:
        budget = entities.budgets.get(campaign.budget_id)
        if budget is not None and budget.explicitly_shared:
            return "shared_member"
    return "own_budget"


def _subject_display_name(entities: _Entities, key: SubjectKey) -> str:
    """Return a decision subject's display name from the loaded entities."""
    subject_type, subject_id = key
    if subject_type == "campaign":
        campaign = entities.campaigns.get(subject_id)
        return f"Campaign #{subject_id}" if campaign is None else campaign.name
    budget = entities.budgets.get(subject_id)
    return f"Budget #{subject_id}" if budget is None else budget.name


async def _load_discrepancies(
    session: AsyncSession,
    ad_ids: Sequence[int],
    entities: _Entities,
    decisions: Mapping[SubjectKey, DecisionInfo],
) -> list[DiscrepancyInfo]:
    """Return the unresolved (``open`` or ``awaiting_platform``) discrepancies, oldest first.

    Each carries its subject's name and decision shape: the shape of its latest decision, or
    the inferred shape of an ``untracked`` subject without one (:func:`_inferred_shape`).
    """
    if not ad_ids:
        return []
    disc = Discrepancy
    stmt = (
        sa.select(
            disc.id,
            disc.source,
            disc.subject_type,
            disc.subject_id,
            disc.kind,
            disc.decided_value,
            disc.platform_value,
            disc.detected_at,
            disc.state,
        )
        .where(disc.dataset_id.in_(ad_ids), disc.state.in_(_OPEN_STATES))
        .order_by(disc.detected_at, disc.id)
    )
    found: list[DiscrepancyInfo] = []
    for row in (await session.execute(stmt)).all():
        disc_id, source, subject_type, subject_id, kind, decided, platform, detected, state = row
        key: SubjectKey = (
            _choice(subject_type, _SUBJECT_TYPES, "discrepancy.subject_type"),
            subject_id,
        )
        decision = decisions.get(key)
        found.append(
            DiscrepancyInfo(
                id=disc_id,
                subject_type=key[0],
                subject_id=subject_id,
                subject_name=_subject_display_name(entities, key),
                source=_choice(source, _SOURCES_ALL, "discrepancy.source"),
                shape=_inferred_shape(entities, key) if decision is None else decision.shape,
                kind=_choice(kind, _DISCREPANCY_KINDS, "discrepancy.kind"),
                decided_value=None if decided is None else MappingProxyType(dict(decided)),
                platform_value=None if platform is None else MappingProxyType(dict(platform)),
                detected_at=detected,
                state=_choice(state, _OPEN_STATES, "discrepancy.state"),
            )
        )
    return found


async def _load_feedback(session: AsyncSession, now: datetime) -> tuple[list[FeedbackInfo], int]:
    """Return active feedback (``until`` after ``now``) and the count of entity-less rows.

    Feedback is keyed by the stable entity key, not by dataset, so it survives a dataset
    transition of the same platform entity. A row without an entity type or key can match no
    item (every item has an entity) and is counted, not loaded.
    """
    fb = RecommendationFeedback
    stmt = (
        sa.select(fb.rule_id, fb.entity_type, fb.entity_id, fb.kind, fb.until)
        .where(fb.until > now)
        .order_by(fb.created_at, fb.id)
    )
    active: list[FeedbackInfo] = []
    skipped = 0
    for rule_id, entity_type, entity_key, kind, until in (await session.execute(stmt)).all():
        if entity_type is None or entity_key is None:
            skipped += 1
            continue
        active.append(
            FeedbackInfo(
                rule_id=rule_id,
                entity_type=entity_type,
                entity_key=entity_key,
                kind=_choice(kind, _FEEDBACK_KINDS, "recommendation_feedback.kind"),
                until=until,
            )
        )
    return active, skipped


async def _load_segment_windows(scope: QueryScope) -> tuple[tuple[date, date], ...]:
    """Return every fetched segment window of the active LinkedIn dataset, ascending."""
    linkedin = scope.dataset_id("linkedin")
    if linkedin is None:
        return ()
    seg = FactSegmentWindow
    stmt = (
        sa.select(seg.window_start, seg.window_end)
        .where(seg.dataset_id == linkedin)
        .distinct()
        .order_by(seg.window_start, seg.window_end)
    )
    return tuple((start, end) for start, end in (await scope.session.execute(stmt)).all())


def _group_days[T](rows: Iterable[tuple[int, T]]) -> dict[int, tuple[T, ...]]:
    """Group ``(entity id, row)`` pairs, already in order, into ``{entity id: rows}``."""
    grouped: dict[int, list[T]] = defaultdict(list)
    for entity_id, item in rows:
        grouped[entity_id].append(item)
    return {entity_id: tuple(items) for entity_id, items in grouped.items()}


async def load_context(
    session: AsyncSession,
    window: AnalysisWindow,
    thresholds: Thresholds,
    *,
    kind: RunKind,
    now: datetime,
) -> AnalysisContext:
    """Assemble one run's :class:`AnalysisContext` from the active datasets.

    AAP 0.5.1 ("Window aggregation") and 0.3.3 (active-dataset filter). The steps, each a
    small set-based query:

    1. Each source's active primary dataset (:func:`active_datasets`); the effective CRM
       dataset of the LinkedIn dataset (:func:`effective_crm_dataset_id`: the companion while
       LinkedIn is simulated) and HubSpot's active primary for the Google-to-CRM link.
    2. ``dataset_freshness`` rows of every dataset read.
    3. Entities of the active Google and LinkedIn datasets.
    4. Daily facts and window totals through :func:`aggregate_sql`, one query per grain;
       :func:`aggregate` reads each grain back from the returned context.
    5. ``entity_snapshot`` observations of campaigns, budgets and conversion actions over the
       window plus a bracketing margin (:func:`_observation_bounds`); for keywords only the
       first and last observation within the window (:func:`_load_keyword_observations`);
       and, independently of the window, each campaign's and budget's newest snapshot at or
       before ``now`` (:func:`_load_latest_observations`), the current state budget
       proposals are sized from.
    6. LinkedIn leads submitted in ``window.utc_bounds()``, with their ``lead_match`` state in
       the effective CRM dataset.
    7. CRM data per CRM dataset, loaded once and shared when the LinkedIn-effective and the
       Google CRM dataset are the same; Google-touch contacts are selected in Google's native
       day (:func:`_load_crm`).
    8. Unresolved discrepancies, with subject names and decision shapes.
    9. The latest decision per subject.
    10. Active feedback (``until`` after ``now``).
    11. Every fetched segment window of the LinkedIn dataset.

    A source without an active dataset yields empty collections, never an error. Nothing is
    written: the session is neither flushed nor committed. One structured log line reports
    row counts per part and the elapsed time; it carries no emails and no contact names.

    Args:
        session: The caller's session.
        window: The analysis window.
        thresholds: The loaded configuration.
        kind: ``default`` or ``custom``.
        now: The run time (tz-aware); window deals and active feedback are judged at it.

    Returns:
        The immutable context of the run.

    Raises:
        ValueError: When ``now`` is naive, ``kind`` is unknown or a stored value breaks an
            enumeration.
        TypeError: When ``now`` is not a ``datetime``.
    """
    started = time.perf_counter()
    if not isinstance(now, datetime):
        raise TypeError(f"now must be a tz-aware datetime, found {now!r}")
    if now.utcoffset() is None:
        raise ValueError("now must be a tz-aware datetime (an instant), found a naive datetime")
    run_kind = _choice(kind, _RUN_KINDS, "kind")

    # 1. Datasets.
    datasets = {
        source: _dataset_ref(row) for source, row in (await active_datasets(session)).items()
    }
    linkedin = datasets.get("linkedin")
    hubspot = datasets.get("hubspot")
    linkedin_crm_id = (
        None if linkedin is None else await effective_crm_dataset_id(session, linkedin.dataset_id)
    )
    google_crm_id = None if hubspot is None else hubspot.dataset_id
    crm_ids = {ds for ds in (linkedin_crm_id, google_crm_id) if ds is not None}
    crm_rows = await _crm_datasets(session, crm_ids)
    scope = QueryScope(
        session=session,
        window=window,
        thresholds=thresholds,
        datasets=MappingProxyType(datasets),
        linkedin_crm_id=linkedin_crm_id if linkedin_crm_id in crm_rows else None,
        google_crm_id=google_crm_id if google_crm_id in crm_rows else None,
        now=now,
    )

    # 2. Freshness of every dataset read.
    read_ids = {ref.dataset_id for ref in datasets.values()} | set(crm_rows)
    freshness = await _load_freshness(session, read_ids)

    # 3. Entities; 4. facts.
    entities = await _load_entities(scope)
    campaign_days = _group_days(
        (day.campaign_id, day.fact)
        for day in (
            *await aggregate_sql(scope, "campaign_day_google"),
            *await aggregate_sql(scope, "campaign_day_linkedin"),
        )
    )
    creative_totals = {
        row.creative_id: row for row in await aggregate_sql(scope, "creative_window")
    }
    keyword_days = _group_days(
        (day.keyword_id, day) for day in await aggregate_sql(scope, "keyword_day")
    )
    search_terms = tuple(await aggregate_sql(scope, "search_term_window"))
    baseline_rows = await aggregate_sql(scope, "google_rate_baseline")
    segments = tuple(await aggregate_sql(scope, "segment_window"))

    # 5. Observations.
    google_timezone = next(
        (acct.timezone for acct in entities.accounts.values() if acct.source == "google"), None
    )
    observations = await _load_observations(scope)
    observations.update(await _load_keyword_observations(scope, google_timezone))
    latest_observations = await _load_latest_observations(scope)

    # 6. Leads; 7. CRM data.
    leads = await _load_leads(scope)
    linkedin_crm: CrmData | None = None
    google_crm: CrmData | None = None
    if scope.linkedin_crm_id is not None and scope.linkedin_crm_id == scope.google_crm_id:
        shared = await _load_crm(
            scope,
            crm_rows[scope.linkedin_crm_id],
            contact_ids=leads.matched_contact_ids,
            google_contacts=True,
            freshness=freshness,
            google_timezone=google_timezone,
        )
        linkedin_crm = google_crm = shared
    else:
        if scope.linkedin_crm_id is not None:
            linkedin_crm = await _load_crm(
                scope,
                crm_rows[scope.linkedin_crm_id],
                contact_ids=leads.matched_contact_ids,
                google_contacts=False,
                freshness=freshness,
                google_timezone=google_timezone,
            )
        if scope.google_crm_id is not None:
            google_crm = await _load_crm(
                scope,
                crm_rows[scope.google_crm_id],
                contact_ids=(),
                google_contacts=True,
                freshness=freshness,
                google_timezone=google_timezone,
            )

    # 8. Discrepancies; 9. decisions; 10. feedback; 11. segment windows.
    ad_ids = scope.ad_dataset_ids()
    decisions = await _load_decisions(session, ad_ids)
    discrepancies = await _load_discrepancies(session, ad_ids, entities, decisions)
    feedback, entityless_feedback = await _load_feedback(session, now)
    segment_windows = await _load_segment_windows(scope)

    context = AnalysisContext(
        window=window,
        thresholds=thresholds,
        now=now,
        run_kind=run_kind,
        datasets=scope.datasets,
        linkedin_crm=linkedin_crm,
        google_crm=google_crm,
        freshness=MappingProxyType(freshness),
        accounts=MappingProxyType(entities.accounts),
        campaign_groups=MappingProxyType(entities.campaign_groups),
        campaigns=MappingProxyType(entities.campaigns),
        budgets=MappingProxyType(entities.budgets),
        ad_groups=MappingProxyType(entities.ad_groups),
        keywords=MappingProxyType(entities.keywords),
        creatives=MappingProxyType(entities.creatives),
        conversion_actions=MappingProxyType(entities.conversion_actions),
        campaign_days=MappingProxyType(campaign_days),
        creative_totals=MappingProxyType(creative_totals),
        keyword_days=MappingProxyType(keyword_days),
        search_terms=search_terms,
        k1_baseline=baseline_rows[0] if baseline_rows else None,
        segments=segments,
        segment_windows=segment_windows,
        observations=MappingProxyType(
            {key: tuple(items) for key, items in sorted(observations.items())}
        ),
        latest_observations=MappingProxyType(dict(sorted(latest_observations.items()))),
        leads=tuple(leads.leads),
        discrepancies=tuple(discrepancies),
        decisions=MappingProxyType(decisions),
        feedback=tuple(feedback),
    )
    _log.info(
        "analysis_context_loaded",
        run_kind=run_kind,
        window_start=window.start.isoformat(),
        window_end=window.end.isoformat(),
        days=window.days,
        dataset_ids={source: ref.dataset_id for source, ref in datasets.items()},
        linkedin_crm_dataset_id=scope.linkedin_crm_id,
        google_crm_dataset_id=scope.google_crm_id,
        counts={
            "freshness": len(freshness),
            "accounts": len(entities.accounts),
            "campaigns": len(entities.campaigns),
            "budgets": len(entities.budgets),
            "budgets_without_currency": entities.budgets_without_currency,
            "ad_groups": len(entities.ad_groups),
            "keywords": len(entities.keywords),
            "creatives": len(entities.creatives),
            "conversion_actions": len(entities.conversion_actions),
            "campaign_days": sum(len(days) for days in campaign_days.values()),
            "creative_totals": len(creative_totals),
            "keyword_days": sum(len(days) for days in keyword_days.values()),
            "search_terms": len(search_terms),
            "segments": len(segments),
            "observations": sum(len(items) for items in observations.values()),
            "latest_observations": len(latest_observations),
            "leads": len(leads.leads),
            "linkedin_crm_contacts": 0 if linkedin_crm is None else len(linkedin_crm.contacts),
            "google_crm_contacts": 0 if google_crm is None else len(google_crm.google_contacts),
            "discrepancies": len(discrepancies),
            "decisions": len(decisions),
            "feedback": len(feedback),
            "feedback_without_entity": entityless_feedback,
            "segment_windows": len(segment_windows),
        },
        elapsed_ms=round((time.perf_counter() - started) * 1000),
    )
    return context


# ---------------------------------------------------------------------------
# Pure half: window aggregation read from the context (AAP 0.5.1)
# ---------------------------------------------------------------------------


@overload
def aggregate(
    ctx: AnalysisContext, grain: Literal["campaign_day_google", "campaign_day_linkedin"]
) -> list[CampaignDay]: ...
@overload
def aggregate(ctx: AnalysisContext, grain: Literal["creative_window"]) -> list[CreativeTotals]: ...
@overload
def aggregate(ctx: AnalysisContext, grain: Literal["keyword_day"]) -> list[KeywordDay]: ...
@overload
def aggregate(
    ctx: AnalysisContext, grain: Literal["search_term_window"]
) -> list[SearchTermRow]: ...
@overload
def aggregate(
    ctx: AnalysisContext, grain: Literal["google_rate_baseline"]
) -> list[RateBaseline]: ...
@overload
def aggregate(ctx: AnalysisContext, grain: Literal["segment_window"]) -> list[SegmentRow]: ...
@overload
def aggregate(ctx: AnalysisContext, grain: Grain) -> list[Any]: ...
def aggregate(ctx: AnalysisContext, grain: Grain) -> list[Any]:
    """Return one grain's window rows from an analysis context, in :func:`aggregate_sql` order.

    AAP 0.5.1 ``aggregate(ctx, grain)``. Pure: no I/O and no logging. :func:`load_context`
    fills the context from :func:`aggregate_sql`, one query per grain; this reads the same
    grain back from the context, so rules, ``precedence.py`` and scenario contexts built in
    memory use one interface. For a context :func:`load_context` returned,
    ``aggregate(ctx, grain)`` equals ``await aggregate_sql(scope, grain)`` over the same
    datasets and window, for every grain. Rows are the context's own (immutable) rows, read
    as stored and never re-filtered by the window.

    * ``campaign_day_google`` and ``campaign_day_linkedin``: a :class:`CampaignDay` per day of
      ``ctx.campaign_days`` whose campaign's ``source`` (``ctx.campaigns``) is the grain's,
      ordered by campaign id, then date.
    * ``creative_window``: the rows of ``ctx.creative_totals``, ordered by creative id.
    * ``keyword_day``: the rows of ``ctx.keyword_days``, ordered by keyword id, then date.
    * ``search_term_window``: ``ctx.search_terms``, in the context's order.
    * ``google_rate_baseline``: ``[ctx.k1_baseline]``, or empty when it is ``None``.
    * ``segment_window``: ``ctx.segments``, in the context's order.

    Sorts are stable, so rows that tie on their sort key keep the context's order.

    Args:
        ctx: The analysis context.
        grain: The grain to read.

    Returns:
        A new list of the grain's rows; empty when the context holds none.

    Raises:
        KeyError: For a campaign-day grain, when ``ctx.campaign_days`` holds a campaign that
            ``ctx.campaigns`` lacks: its source, and so its grain, is unknown, and its days
            are never silently dropped from either grain.
        ValueError: For an unknown grain.
    """
    match grain:
        case "campaign_day_google":
            return _context_campaign_days(ctx, "google")
        case "campaign_day_linkedin":
            return _context_campaign_days(ctx, "linkedin")
        case "creative_window":
            return sorted(ctx.creative_totals.values(), key=lambda row: row.creative_id)
        case "keyword_day":
            return sorted(
                (day for days in ctx.keyword_days.values() for day in days),
                key=lambda day: (day.keyword_id, day.date),
            )
        case "search_term_window":
            return list(ctx.search_terms)
        case "google_rate_baseline":
            return [] if ctx.k1_baseline is None else [ctx.k1_baseline]
        case "segment_window":
            return list(ctx.segments)
    raise ValueError(f"grain must be one of {GRAINS}, found {grain!r}")


def _context_campaign_days(
    ctx: AnalysisContext, source: Literal["google", "linkedin"]
) -> list[CampaignDay]:
    """Return ``source``'s campaign days of the context, ordered by campaign id, then date.

    Raises:
        KeyError: When ``ctx.campaign_days`` holds a campaign that ``ctx.campaigns`` lacks.
    """
    rows: list[CampaignDay] = []
    for campaign_id in sorted(ctx.campaign_days):
        if _campaign(ctx, campaign_id).source != source:
            continue
        days = sorted(ctx.campaign_days[campaign_id], key=lambda day: day.date)
        rows.extend(CampaignDay(campaign_id, day) for day in days)
    return rows


# ---------------------------------------------------------------------------
# Pure half: outcome, efficiency and baselines (AAP 0.5.4.1)
# ---------------------------------------------------------------------------


def outcome(day: DayFact, objective_group: str) -> Decimal | None:
    """Return a day's outcome for an objective group (AAP 0.5.4.1 "Outcome by source").

    Google → ``conversions``; LinkedIn ``LEAD_GENERATION`` → ``leads`` (``oneClickLeads``);
    other LinkedIn objectives → ``website_conversions`` (``externalWebsiteConversions``).

    Returns:
        The outcome, or ``None`` when the day did not report it ("unavailable", never 0).

    Raises:
        KeyError: For an unknown objective group.
    """
    return day.outcome(objective_group)


@dataclass(frozen=True, slots=True)
class Totals:
    """Totals of one or more campaigns of one source, objective group and currency.

    Attributes:
        spend: Spend in the account currency.
        outcomes: Outcomes of the objective group (:func:`outcome`). ``None`` ("unavailable",
            never zero) when there is no fact row, or when any fact row did not report its
            outcome: its spend still counts, so a sum of the reported days alone would set
            an incomplete count against the full spend (AAP 0.11.1).
        impressions: Impressions.
        clicks: Clicks.
        spend_days: Native dates with spend > 0 (summed across the campaigns).
        days: Native dates with any fact row.
        unreported_days: Native dates on which some fact row did not report its outcome,
            so evidence can say why ``outcomes`` is unavailable ("on 2 of 28 days").
    """

    spend: Decimal
    outcomes: Decimal | None
    impressions: int
    clicks: int
    spend_days: int
    days: int
    unreported_days: int = 0


def _campaign(ctx: AnalysisContext, campaign_id: int) -> CampaignInfo:
    """Return a context campaign, or raise ``KeyError`` naming it."""
    campaign = ctx.campaigns.get(campaign_id)
    if campaign is None:
        raise KeyError(f"campaign {campaign_id} is not in the analysis context")
    return campaign


def _totals(
    ctx: AnalysisContext, parts: Iterable[tuple[CampaignInfo, Collection[date] | None]]
) -> Totals:
    """Sum window facts of ``(campaign, allowed dates or None for all)`` pairs.

    Outcomes are summed only when every selected fact row reported its objective-group
    outcome; one unreported row makes them ``None`` and is counted in
    :attr:`Totals.unreported_days`, because its spend is in the total either way.

    Raises:
        ValueError: When the campaigns span more than one source, objective group or
            currency, which AAP 0.5.4.1 never compares or sums together.
    """
    pairs = list(parts)
    groups = {(c.source, c.objective_group) for c, _ in pairs}
    currencies = {ctx.account_currency(c.id) for c, _ in pairs}
    if len(groups) > 1 or len(currencies) > 1:
        raise ValueError(
            "totals stay within one source, objective group and currency; found "
            f"groups {sorted(groups)} and currencies {sorted(currencies)}"
        )
    spend = _ZERO
    outcomes = _ZERO
    unreported: set[date] = set()
    impressions = clicks = 0
    by_date: dict[date, Decimal] = {}
    window = ctx.window
    for campaign, allowed in pairs:
        allowed_set = None if allowed is None else frozenset(allowed)
        for day in ctx.campaign_days.get(campaign.id, ()):
            if not window.start <= day.date <= window.end:
                continue
            if allowed_set is not None and day.date not in allowed_set:
                continue
            spend += day.cost
            impressions += day.impressions
            clicks += day.clicks
            by_date[day.date] = by_date.get(day.date, _ZERO) + day.cost
            value = day.outcome(campaign.objective_group)
            if value is None:
                unreported.add(day.date)
            else:
                outcomes += value
    return Totals(
        spend=spend,
        outcomes=outcomes if by_date and not unreported else None,
        impressions=impressions,
        clicks=clicks,
        spend_days=sum(1 for cost in by_date.values() if cost > 0),
        days=len(by_date),
        unreported_days=len(unreported),
    )


def window_totals(
    ctx: AnalysisContext, campaign_ids: Iterable[int], *, dates: Collection[date] | None = None
) -> Totals:
    """Return window totals of campaigns of one source, objective group and currency.

    Only facts dated inside the window count, so a context carrying lookback days for other
    purposes still totals the window alone. Outcomes are ``None`` unless every counted fact
    row reported them (:class:`Totals`).

    Args:
        ctx: The analysis context.
        campaign_ids: The campaigns to total; repeats are ignored.
        dates: When given, only these native dates count (a shared-budget member's days).

    Raises:
        KeyError: When a campaign is not in the context.
        ValueError: When the campaigns span sources, objective groups or currencies.
    """
    campaigns = [_campaign(ctx, cid) for cid in sorted(set(campaign_ids))]
    return _totals(ctx, ((campaign, dates) for campaign in campaigns))


def cost_per_outcome(spend: Decimal, outcomes: Decimal | None) -> Decimal | None:
    """Return spend ÷ outcomes, or ``None`` at 0 or unavailable outcomes (AAP 0.5.4.1)."""
    if outcomes is None or outcomes <= 0:
        return None
    return spend / outcomes


def expected_outcomes(spend: Decimal, baseline_cpo: Decimal | None) -> Decimal | None:
    """Return spend ÷ the baseline cost per outcome: outcomes an average entity would show.

    ``None`` when the baseline is unavailable or not positive (AAP 0.5.4.1).
    """
    if baseline_cpo is None or baseline_cpo <= 0:
        return None
    return spend / baseline_cpo


def baseline_cpo(ctx: AnalysisContext, account_id: int, objective_group: str) -> Decimal | None:
    """Return the account's average cost per outcome for one objective group over the window.

    AAP 0.5.4.1 "Efficiency": Σ spend ÷ Σ outcomes across the account's campaigns of that
    group, over the same window. One account means one source and one currency, so the
    baseline never crosses either. Memoised in ``ctx.cache``.

    Returns:
        The baseline, or ``None`` when the group's outcomes are 0 or unavailable, including
        when any of its campaigns did not report outcomes on a window day.
    """
    key = f"metrics.baseline_cpo:{account_id}:{objective_group}"
    if key in ctx.cache:
        cached: Decimal | None = ctx.cache[key]
        return cached
    totals = _baseline_totals(ctx, account_id, objective_group)
    value = cost_per_outcome(totals.spend, totals.outcomes)
    ctx.cache[key] = value
    return value


def _baseline_totals(ctx: AnalysisContext, account_id: int, objective_group: str) -> Totals:
    """Return the account-group window totals :func:`baseline_cpo` divides; memoised.

    The exact B2 and G1 comparisons (:func:`_poor_branch`) read the baseline as these totals'
    spend and outcomes, so they and the displayed quotient describe the same average.
    """
    key = f"metrics.baseline_totals:{account_id}:{objective_group}"
    cached = ctx.cache.get(key)
    if isinstance(cached, Totals):
        return cached
    members = [
        campaign
        for campaign in ctx.campaigns.values()
        if campaign.account_id == account_id and campaign.objective_group == objective_group
    ]
    totals = _totals(ctx, ((campaign, None) for campaign in members))
    ctx.cache[key] = totals
    return totals


def source_totals(ctx: AnalysisContext) -> dict[Source, tuple[Decimal, Decimal]]:
    """Return ``{source: (window spend, window outcomes)}`` for Google and LinkedIn.

    The input of P2's ``conversion_tracking_absent`` check (AAP 0.5.4.2): a source with spend
    > 0 and zero outcomes across the window. Outcomes are each campaign's own objective-group
    outcome; an unreported value adds nothing, because P2 asks whether any outcome was
    *recorded*. A source appears when it has an active dataset or campaigns in the context.
    """
    totals: dict[Source, tuple[Decimal, Decimal]] = {}
    for source in _AD_SOURCES:
        if source in ctx.datasets or any(c.source == source for c in ctx.campaigns.values()):
            totals[source] = (_ZERO, _ZERO)
    window = ctx.window
    for campaign in ctx.campaigns.values():
        spend, outcomes = totals.get(campaign.source, (_ZERO, _ZERO))
        for day in ctx.campaign_days.get(campaign.id, ()):
            if not window.start <= day.date <= window.end:
                continue
            spend += day.cost
            value = day.outcome(campaign.objective_group)
            if value is not None:
                outcomes += value
        totals[campaign.source] = (spend, outcomes)
    return totals


# ---------------------------------------------------------------------------
# Pure half: budget-day classification (AAP 0.5.4.1 "Budget history")
# ---------------------------------------------------------------------------

DayClass = Literal["known", "changed_intraday", "unobserved"]
"""How much the observations around a native day establish about it (AAP 0.5.4.1).

``known``: whole-day known; ``changed_intraday``: the bracketing observations disagree, so the
effective time of the change is unknown; ``unobserved``: a bracketing observation is missing
or a gap between observations exceeds ``budget.max_observation_gap_hours``.
"""


@lru_cache(maxsize=8192)
def _day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """Memoised :meth:`AnalysisWindow.day_bounds`; the same days repeat for every entity.

    Bounded, so a long-running worker never grows it: 8,192 entries hold several years of
    native days in each of a few zones.
    """
    return AnalysisWindow.day_bounds(day, tz)


@dataclass(frozen=True, slots=True)
class _Span:
    """One native day's classification, with the observation span it was read from.

    ``first`` and ``last`` index the sorted observations: the last at or before the day start
    and the first at or after the day end, clamped to the list (``-1`` when it is empty).
    """

    klass: DayClass
    value: Hashable | None
    first: int
    last: int


@dataclass(frozen=True, slots=True)
class _Series:
    """One entity's observations sorted by instant, with their instants and values."""

    observations: tuple[Observation, ...]
    times: tuple[datetime, ...]
    values: tuple[Hashable, ...]

    @classmethod
    def of(
        cls, observations: Sequence[Observation], value: Callable[[Observation], Hashable]
    ) -> _Series:
        """Sort ``observations`` by ``captured_at`` (stable, so input order breaks ties)."""
        ordered = tuple(sorted(observations, key=lambda obs: obs.captured_at))
        return cls(ordered, tuple(obs.captured_at for obs in ordered), tuple(map(value, ordered)))

    def span(self, lower: datetime, upper: datetime, gap: timedelta) -> _Span:
        """Classify the day ``[lower, upper)`` exactly as AAP 0.5.4.1 defines it."""
        count = len(self.times)
        first = bisect_right(self.times, lower) - 1
        last = bisect_left(self.times, upper)
        clamped_first, clamped_last = max(first, 0), min(last, count - 1)
        if first < 0 or last >= count:
            return _Span("unobserved", None, clamped_first, clamped_last)
        for index in range(first, last):
            if self.times[index + 1] - self.times[index] > gap:
                return _Span("unobserved", None, first, last)
        head = self.values[first]
        if all(self.values[index] == head for index in range(first + 1, last + 1)):
            return _Span("known", head, first, last)
        return _Span("changed_intraday", None, first, last)


def _require_gap_hours(max_gap_hours: object) -> timedelta:
    """Validate ``budget.max_observation_gap_hours`` (a positive integer).

    Raises:
        ValueError: When it is not an ``int`` of at least 1 (``bool`` rejected).
    """
    if isinstance(max_gap_hours, bool) or not isinstance(max_gap_hours, int) or max_gap_hours < 1:
        raise ValueError(f"max_gap_hours must be a positive integer, found {max_gap_hours!r}")
    return timedelta(hours=max_gap_hours)


def _classify(series: _Series, dates: Sequence[date], tz: str, gap: timedelta) -> dict[date, _Span]:
    """Classify every date of ``dates`` from one entity's sorted observations."""
    spans: dict[date, _Span] = {}
    for day in dates:
        lower, upper = _day_bounds(day, tz)
        spans[day] = series.span(lower, upper, gap)
    return spans


def classify_days(
    observations: Sequence[Observation],
    dates: Sequence[date],
    tz: str,
    *,
    value: Callable[[Observation], Hashable],
    max_gap_hours: int,
) -> dict[date, tuple[DayClass, Hashable | None]]:
    """Classify each native day from the observations around it (AAP 0.5.4.1).

    For native day ``d`` = ``[d 00:00, d + 1 00:00)`` in ``tz``
    (:meth:`AnalysisWindow.day_bounds`, so a DST day has its true 23 or 25 hours):

    1. Find the **last** observation at or before the day start and the **first** at or
       after the day end. When either is missing the day is ``unobserved``: for example the
       first snapshot was taken at noon, or the day precedes the first snapshot.
    2. When any gap between consecutive observations of that span exceeds
       ``max_gap_hours`` (``budget.max_observation_gap_hours``, 6) the day is ``unobserved``:
       a sync failed for longer than the limit.
    3. Otherwise, when every observation of the span has the same ``value(obs)``, the day is
       ``known`` with that value. The stated assumption is that the value did not change and
       revert between two observations at most ``max_gap_hours`` apart; evidence names it
       as "unchanged across N observations, at most 6 h apart".
    4. Otherwise the day is ``changed_intraday``: for example a budget raised at noon. The
       effective time of the change is unknown, so the day carries no value.

    Unknown days (``changed_intraday`` and ``unobserved``) are never assigned an observed
    value; budget rules exclude them from utilization and cap counts and count them
    separately.

    Args:
        observations: One entity's observations, in any order (sorted by ``captured_at``).
        dates: The native dates to classify.
        tz: The IANA zone of the source's native day (Google: the account's zone; LinkedIn:
            ``UTC``).
        value: What must stay unchanged, for example :func:`campaign_state_value`.
        max_gap_hours: ``budget.max_observation_gap_hours``.

    Returns:
        ``{date: (class, value)}``; the value is ``None`` unless the class is ``known``.

    Raises:
        ValueError: When ``max_gap_hours`` is not a positive integer or ``tz`` is not a zone.
        TypeError: When a date is not a native ``date``.
    """
    gap = _require_gap_hours(max_gap_hours)
    spans = _classify(_Series.of(observations, value), dates, tz, gap)
    return {day: (span.klass, span.value) for day, span in spans.items()}


def _period_of(obs: Observation) -> str | None:
    """The budget period an observation states: payload ``period`` or ``budget_period``."""
    raw = obs.payload.get("period", obs.payload.get("budget_period"))
    return raw if isinstance(raw, str) else None


def _daily_amount(obs: Observation) -> Decimal | None:
    """The daily budget an observation shows, at native precision; ``None`` when not daily.

    Google amounts come from ``budget_amount_micros`` (``Decimal(micros) / 1,000,000`` is
    exact, and equal Decimals compare and hash equal, so this is a native-precision
    comparison). A ``CUSTOM_PERIOD`` amount is a lifetime amount, never a daily one. Without a
    stated period, ``budget_daily`` decides: the snapshot writer fills it only for ``DAILY``.
    """
    period = _period_of(obs)
    if period == "CUSTOM_PERIOD":
        return None
    if period is None and obs.budget_daily is None:
        return None
    if obs.budget_amount_micros is not None:
        return Decimal(obs.budget_amount_micros) / _MICROS
    return obs.budget_daily


def campaign_state_value(obs: Observation) -> tuple[str | None, Decimal | None]:
    """A campaign's ``(status, own daily budget)`` at one observation.

    Google: the daily amount of the campaign's own ``DAILY`` budget from
    ``budget_amount_micros``; ``None`` while it drew from an explicitly shared budget (whose
    cap belongs to the budget subject) or from a ``CUSTOM_PERIOD`` budget. LinkedIn:
    ``budget_daily`` (``dailyBudget``).
    """
    if obs.payload.get("explicitly_shared") is True:
        return (obs.status, None)
    return (obs.status, _daily_amount(obs))


def budget_amount_value(obs: Observation) -> Decimal | None:
    """A Google shared budget's daily amount at one observation (``None`` when not daily)."""
    return _daily_amount(obs)


def membership_value(obs: Observation) -> int | None:
    """The Google budget a campaign drew from at one observation (dated membership)."""
    return obs.budget_id


def status_value(obs: Observation) -> str | None:
    """An entity's status at one observation."""
    return obs.status


# ---------------------------------------------------------------------------
# Pure half: budget histories (AAP 0.5.4.1, 0.5.4.5)
# ---------------------------------------------------------------------------

BUDGET_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "budget.max_observation_gap_hours",
    "budget.capped_day_ratio",
    "budget.capped_lost_is_share",
    "budget.high_utilization",
    "budget.under_utilization",
    "budget.min_known_days",
    "budget.min_spending_days",
    "budget.min_capped_days",
    "budget.sustained_share",
)
"""Every ``budget.*`` key a budget history and its gates apply, for ``evidence.thresholds``."""


@dataclass(frozen=True, slots=True)
class GateConfig:
    """The ``budget.*`` values a history was classified with and its gates apply.

    Attributes:
        max_gap_hours: ``budget.max_observation_gap_hours`` (6).
        capped_day_ratio: ``budget.capped_day_ratio`` (0.95).
        capped_lost_is_share: ``budget.capped_lost_is_share`` (0.10): Google's alternative
            capped-day test on ``search_budget_lost_impression_share``.
        high_utilization: ``budget.high_utilization`` (0.90).
        under_utilization: ``budget.under_utilization`` (0.50).
        min_known_days: ``budget.min_known_days`` (7).
        min_spending_days: ``budget.min_spending_days`` (5).
        min_capped_days: ``budget.min_capped_days`` (5).
        sustained_share: ``budget.sustained_share`` (0.70).
    """

    max_gap_hours: int
    capped_day_ratio: Decimal
    capped_lost_is_share: Decimal
    high_utilization: Decimal
    under_utilization: Decimal
    min_known_days: int
    min_spending_days: int
    min_capped_days: int
    sustained_share: Decimal

    @classmethod
    def from_thresholds(cls, thresholds: Thresholds) -> GateConfig:
        """Read every value from ``config/thresholds.yaml``; there is no code default.

        Raises:
            ConfigError: When a key is missing or mistyped.
        """
        return cls(
            max_gap_hours=thresholds.int("budget.max_observation_gap_hours"),
            capped_day_ratio=thresholds.ratio("budget.capped_day_ratio"),
            capped_lost_is_share=thresholds.ratio("budget.capped_lost_is_share"),
            high_utilization=thresholds.ratio("budget.high_utilization"),
            under_utilization=thresholds.ratio("budget.under_utilization"),
            min_known_days=thresholds.int("budget.min_known_days"),
            min_spending_days=thresholds.int("budget.min_spending_days"),
            min_capped_days=thresholds.int("budget.min_capped_days"),
            sustained_share=thresholds.ratio("budget.sustained_share"),
        )


def _gate_config(ctx: AnalysisContext) -> GateConfig:
    """The run's :class:`GateConfig`, read once per context."""
    key = "metrics.gate_config"
    cached = ctx.cache.get(key)
    if isinstance(cached, GateConfig):
        return cached
    config = GateConfig.from_thresholds(ctx.thresholds)
    ctx.cache[key] = config
    return config


@dataclass(frozen=True, slots=True)
class BudgetDay:
    """One native day of a budget subject's history.

    A **known-budget active day** (``budget-active`` for a shared budget) is ``known`` with
    status ``active`` and a daily budget above 0; only such days carry ``u_d`` and the daily
    predicates. ``spending``: a known-budget active day with spend > 0. ``idle``: a
    known-budget active day with zero spend, a genuine observation distinct from an unknown
    day.

    Attributes:
        date: The native date.
        klass: ``known``, ``changed_intraday`` or ``unobserved``.
        status: The whole-day status; ``None`` unless known.
        daily_budget: The whole-day daily budget; ``None`` unless known and daily.
        spend: The day's spend from the facts (0 without a fact row). Shown, never used for
            utilization on an unknown day.
        u_d: Daily utilization = spend ÷ daily budget, on known-budget active days only.
        capped: ``u_d ≥ budget.capped_day_ratio``, or for Google
            ``search_budget_lost_impression_share ≥ budget.capped_lost_is_share``.
        high: ``u_d ≥ budget.high_utilization``.
        under: ``u_d ≤ budget.under_utilization`` (idle days included).
        spending: A known-budget active day with spend > 0.
        idle: A known-budget active day with zero spend.
    """

    date: date
    klass: DayClass
    status: str | None
    daily_budget: Decimal | None
    spend: Decimal
    u_d: Decimal | None
    capped: bool
    high: bool
    under: bool
    spending: bool
    idle: bool

    @property
    def known_active(self) -> bool:
        """Whether this is a known-budget (or budget-active) day."""
        return self.u_d is not None


def _unknown_day(day: date, klass: DayClass, spend: Decimal) -> BudgetDay:
    """A ``changed_intraday`` or ``unobserved`` day: no value, no predicate."""
    return BudgetDay(day, klass, None, None, spend, None, False, False, False, False, False)


def _inactive_day(
    day: date, status: str | None, daily: Decimal | None, spend: Decimal
) -> BudgetDay:
    """A whole-day-known day that is not a known-budget active day (paused, or no daily cap)."""
    return BudgetDay(day, "known", status, daily, spend, None, False, False, False, False, False)


def _active_day(
    day: date,
    status: str | None,
    daily: Decimal,
    spend: Decimal,
    lost_is: Decimal | None,
    config: GateConfig,
) -> BudgetDay:
    """A known-budget active day with its utilization and daily predicates (AAP 0.5.4.1)."""
    u_d = spend / daily
    capped = u_d >= config.capped_day_ratio or (
        lost_is is not None and lost_is >= config.capped_lost_is_share
    )
    return BudgetDay(
        date=day,
        klass="known",
        status=status,
        daily_budget=daily,
        spend=spend,
        u_d=u_d,
        capped=capped,
        high=u_d >= config.high_utilization,
        under=u_d <= config.under_utilization,
        spending=spend > 0,
        idle=spend == 0,
    )


@dataclass(frozen=True, slots=True)
class BudgetHistory:
    """A budget subject's classified window days and their counts (AAP 0.5.4.1, 0.5.4.5).

    Attributes:
        subject: ``("campaign", id)`` for an own-budget campaign, ``("budget", id)`` for a
            Google explicitly shared budget.
        shared: Whether the subject is a shared budget; its gates count budget-active days.
        utilization_applicable: Whether the subject has a daily budget (Google ``DAILY`` or a
            LinkedIn ``dailyBudget``). Lifetime and custom-period budgets show utilization as
            "n/a" and have no known-budget active day, so every cap rule excludes them.
        days: One :class:`BudgetDay` per window date, ascending.
        known_active: Known-budget (or budget-active) days.
        spending: Spending days.
        idle: Known idle days.
        changed_intraday: Days whose observations disagree.
        unobserved: Days without bracketing observations, with an over-long gap, or with no
            fact row while the metrics dataset is not verified ``complete`` over the date.
        capped: Capped days.
        high: High-utilization days.
        under: Under-utilized days.
        window_utilization: Σ spend ÷ Σ daily budget over known-budget active days; ``None``
            without such a day.
        latest_daily_budget: The latest effective daily budget, the current amount B1, B2
            and B3 are sized from: the daily amount of the subject's newest observation
            captured at or before the run time, from its window series or
            ``ctx.latest_observations``, whichever is newer. ``None`` when there is no such
            observation or it shows no daily amount (for example the campaign now draws from
            a shared or ``CUSTOM_PERIOD`` budget), never an older amount. The classified days
            keep their own historical values.
        members: Shared budgets: each member campaign's member days; empty for a campaign.
        observation_count: Observations the known days were read from (the N of
            :attr:`assumption`).
        config: The ``budget.*`` values the history was classified with.
    """

    subject: SubjectKey
    shared: bool
    utilization_applicable: bool
    days: tuple[BudgetDay, ...]
    known_active: int
    spending: int
    idle: int
    changed_intraday: int
    unobserved: int
    capped: int
    high: int
    under: int
    window_utilization: Decimal | None
    latest_daily_budget: Decimal | None
    members: Mapping[int, tuple[date, ...]]
    observation_count: int
    config: GateConfig

    @property
    def known_label(self) -> str:
        """The evidence label of :attr:`known_active`."""
        return "budget-active days" if self.shared else "known-budget days"

    @property
    def assumption(self) -> str:
        """The stated observation assumption, for ``evidence.extra.observation_assumption``."""
        return (
            f"unchanged across {self.observation_count} observations, at most "
            f"{self.config.max_gap_hours} h apart"
        )

    @property
    def window_spend(self) -> Decimal:
        """Spend over every window day of the history (members only, for a shared budget)."""
        return sum((day.spend for day in self.days), _ZERO)

    def counts(self) -> dict[str, int]:
        """Every day count, keyed for evidence: known-budget active, spending, idle and so on."""
        return {
            "known_active": self.known_active,
            "spending": self.spending,
            "idle": self.idle,
            "changed_intraday": self.changed_intraday,
            "unobserved": self.unobserved,
            "capped": self.capped,
            "high": self.high,
            "under": self.under,
        }


def _summarise(
    subject: SubjectKey,
    *,
    shared: bool,
    applicable: bool,
    days: Sequence[BudgetDay],
    latest: Decimal | None,
    members: Mapping[int, tuple[date, ...]],
    observation_count: int,
    config: GateConfig,
) -> BudgetHistory:
    """Count a subject's days into a :class:`BudgetHistory`."""
    active = [day for day in days if day.known_active]
    budget_total = sum((day.daily_budget or _ZERO for day in active), _ZERO)
    spend_total = sum((day.spend for day in active), _ZERO)
    return BudgetHistory(
        subject=subject,
        shared=shared,
        utilization_applicable=applicable,
        days=tuple(days),
        known_active=len(active),
        spending=sum(1 for day in active if day.spending),
        idle=sum(1 for day in active if day.idle),
        changed_intraday=sum(1 for day in days if day.klass == "changed_intraday"),
        unobserved=sum(1 for day in days if day.klass == "unobserved"),
        capped=sum(1 for day in active if day.capped),
        high=sum(1 for day in active if day.high),
        under=sum(1 for day in active if day.under),
        window_utilization=spend_total / budget_total if budget_total > 0 else None,
        latest_daily_budget=latest,
        members=MappingProxyType(dict(sorted(members.items()))),
        observation_count=observation_count,
        config=config,
    )


def _source_tz(ctx: AnalysisContext, source: Source) -> str:
    """The native-day zone of ``source``: the window's boundary, else the account zone or UTC."""
    boundary = None if source == "hubspot" else _window_boundary(ctx.window, source)
    if boundary is not None:
        return boundary
    if source == "google":
        for account in sorted(ctx.accounts.values(), key=lambda acct: acct.id):
            if account.source == "google" and account.timezone:
                return account.timezone
    return UTC_BOUNDARY


def _facts_by_date(ctx: AnalysisContext, campaign_id: int) -> Mapping[date, DayFact]:
    """A campaign's window facts keyed by native date (memoised)."""
    key = f"metrics.facts_by_date:{campaign_id}"
    cached = ctx.cache.get(key)
    if isinstance(cached, Mapping):
        return cached
    window = ctx.window
    facts = MappingProxyType(
        {
            day.date: day
            for day in ctx.campaign_days.get(campaign_id, ())
            if window.start <= day.date <= window.end
        }
    )
    ctx.cache[key] = facts
    return facts


def _metrics_covered(ctx: AnalysisContext, source: Source, day: date) -> bool:
    """Whether the source's daily metrics dataset verifiably covers ``day`` completely.

    Coverage is verified only when the dataset's freshness row exists, is ``complete``, and
    has both ``covered_from`` and ``covered_through`` set with ``day`` between them.
    Completeness is recorded over the whole covered range, so a ``complete`` range holding
    ``day`` is the proof that every fact row of that date was fetched. Only then does a
    missing fact row mean zero spend. An ``incomplete`` or ``unknown`` row (for example a
    call-budget deferral leaving partitions unfetched), an open-ended range or a date outside
    it leaves the day's spend unknown, and the day counts as unobserved (AAP 0.5.4.1: unknown
    days are never assigned an observed value and never counted idle).
    """
    name = _METRICS_DATASETS.get(source)
    if name is None:
        return False
    row = ctx.freshness_for(name, ctx.dataset_id_for(name, crm_scope=None))
    return (
        row is not None
        and row.is_complete
        and row.covered_from is not None
        and row.covered_through is not None
        and row.covered_from <= day <= row.covered_through
    )


def _draws_from_shared(ctx: AnalysisContext, campaign: CampaignInfo) -> int | None:
    """The explicitly shared budget a Google campaign draws from now, or ``None``."""
    if campaign.source != "google" or campaign.budget_id is None:
        return None
    budget = ctx.budgets.get(campaign.budget_id)
    if budget is None or not budget.explicitly_shared:
        return None
    return budget.id


def _utilization_applicable(campaign: CampaignInfo) -> bool:
    """Whether an own-budget campaign has a daily cap (Google ``DAILY``, LinkedIn daily)."""
    if campaign.source == "google":
        return campaign.budget_period == "DAILY"
    return campaign.daily_budget is not None


def _span_indices(spans: Iterable[_Span]) -> set[int]:
    """The observation indices that known days were read from."""
    used: set[int] = set()
    for span in spans:
        if span.klass == "known":
            used.update(range(span.first, span.last + 1))
    return used


def _latest_daily(
    ctx: AnalysisContext,
    subject: SubjectKey,
    series: _Series,
    value_of: Callable[[Observation], Hashable],
    daily_of: Callable[[Hashable], Decimal | None],
) -> Decimal | None:
    """The latest effective daily budget (AAP 0.5.4.5 B1): the subject's current amount.

    The candidates are the newest observation of the window series captured at or before
    ``ctx.now`` and ``ctx.latest_observations[subject]``, the newest stored snapshot read
    independently of the window; the newer one wins (the stored one on a tie, the same row
    when both come from the database). Its daily amount is ``daily_of(value_of(obs))``, the
    value function the history classifies with. Historical day values never stand in: a
    window observed at 100 under a newer snapshot at 200 yields 200. When the newest
    observation shows no daily amount, for example the campaign now draws from a shared or
    ``CUSTOM_PERIOD`` budget, the current amount is unavailable (``None``) and no older
    amount replaces it, so no proposal is sized from an obsolete budget.
    """
    newest: Observation | None = None
    newest_value: Hashable = None
    position = bisect_right(series.times, ctx.now)
    if position > 0:
        newest, newest_value = series.observations[position - 1], series.values[position - 1]
    stored = ctx.latest_observations.get(subject)
    if (
        stored is not None
        and stored.captured_at <= ctx.now
        and (newest is None or stored.captured_at >= newest.captured_at)
    ):
        newest, newest_value = stored, value_of(stored)
    return None if newest is None else daily_of(newest_value)


def _campaign_daily(value: Hashable) -> Decimal | None:
    """The daily budget component of a :func:`campaign_state_value`."""
    if isinstance(value, tuple) and len(value) == 2:
        amount = value[1]
        return amount if isinstance(amount, Decimal) else None
    return None


def _campaign_status(value: Hashable) -> str | None:
    """The status component of a :func:`campaign_state_value`."""
    if isinstance(value, tuple) and len(value) == 2:
        status = value[0]
        return status if isinstance(status, str) else None
    return None


def _shared_daily(value: Hashable) -> Decimal | None:
    """A :func:`budget_amount_value`."""
    return value if isinstance(value, Decimal) else None


def campaign_budget_history(ctx: AnalysisContext, campaign_id: int) -> BudgetHistory:
    """Return an own-budget campaign's budget history over the window (AAP 0.5.4.1).

    Each native day (Google in the account's zone, LinkedIn UTC) is classified from the
    campaign's observations with :func:`campaign_state_value` and
    ``budget.max_observation_gap_hours``. A whole-day-known day with status ``active`` and a
    daily budget above 0 is a known-budget active day: its spend is the day's fact, or 0 when
    no fact row exists and the source's metrics dataset is ``complete`` over a covered range
    holding the date; with no fact row and no such verified coverage (``incomplete`` or
    ``unknown`` completeness, a missing bound, or a date outside the range) the day is
    unobserved instead. Changed-intraday and unobserved days are excluded from utilization and
    cap counts and counted separately.

    Utilization applies only to daily budgets: Google ``DAILY`` own budgets and LinkedIn
    campaigns with ``dailyBudget``. Otherwise ``utilization_applicable`` is false and no day
    is a known-budget active day.

    ``latest_daily_budget`` is the current amount, not a historical one: the own daily budget
    of the campaign's newest observation at or before ``ctx.now``, from its window series or
    ``ctx.latest_observations``, whichever is newer; ``None`` when that observation shows no
    own daily budget (:func:`_latest_daily`). Memoised in ``ctx.cache``.

    Raises:
        KeyError: When the campaign is not in the context.
        ValueError: When the campaign draws from an explicitly shared budget: its budget is
            evaluated at budget level (:func:`shared_budget_history`), never per member.
    """
    key = f"metrics.campaign_history:{campaign_id}"
    cached = ctx.cache.get(key)
    if isinstance(cached, BudgetHistory):
        return cached
    campaign = _campaign(ctx, campaign_id)
    shared_budget = _draws_from_shared(ctx, campaign)
    if shared_budget is not None:
        raise ValueError(
            f"campaign {campaign_id} draws from explicitly shared budget {shared_budget}; "
            "evaluate the budget subject with shared_budget_history"
        )
    config = _gate_config(ctx)
    tz = _source_tz(ctx, campaign.source)
    dates = ctx.window.dates()
    series = _Series.of(ctx.observations.get(("campaign", campaign_id), ()), campaign_state_value)
    spans = _classify(series, dates, tz, timedelta(hours=config.max_gap_hours))
    facts = _facts_by_date(ctx, campaign_id)
    applicable = _utilization_applicable(campaign)
    days: list[BudgetDay] = []
    for day in dates:
        span = spans[day]
        fact = facts.get(day)
        spend = _ZERO if fact is None else fact.cost
        if span.klass != "known":
            days.append(_unknown_day(day, span.klass, spend))
            continue
        status_text, daily_amount = _campaign_status(span.value), _campaign_daily(span.value)
        if not applicable or status_text != "active" or daily_amount is None or daily_amount <= 0:
            days.append(_inactive_day(day, status_text, daily_amount, spend))
            continue
        if fact is None and not _metrics_covered(ctx, campaign.source, day):
            days.append(_unknown_day(day, "unobserved", spend))
            continue
        lost_is = fact.budget_lost_is if fact is not None and campaign.source == "google" else None
        days.append(_active_day(day, status_text, daily_amount, spend, lost_is, config))
    history = _summarise(
        ("campaign", campaign_id),
        shared=False,
        applicable=applicable,
        days=days,
        latest=_latest_daily(
            ctx, ("campaign", campaign_id), series, campaign_state_value, _campaign_daily
        ),
        members={},
        observation_count=len(_span_indices(spans.values())),
        config=config,
    )
    ctx.cache[key] = history
    return history


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A campaign that showed a shared budget in some observation, classified two ways."""

    series: _Series
    membership: Mapping[date, _Span]
    status: Mapping[date, _Span]


def _shows_budget(
    series: _Series,
    lower: datetime,
    upper: datetime,
    gap: timedelta,
    budget_id: int,
    *,
    spent: bool,
) -> bool:
    """Whether a campaign showed ``budget_id`` in an observation bracketing day ``[lower, upper)``.

    The bracket is AAP 0.5.4.1's: from the last observation at or before the day start
    through the first at or after the day end (whichever exist). An observation more than
    ``budget.max_observation_gap_hours`` outside the day counts only when the campaign spent
    on the day: such an observation can never be part of a whole-day-known span, and a
    campaign with no observation near the day while the budget itself was whole-day known
    did not exist (or was not tracked) then, so it cannot unsettle the day. A campaign that
    did spend is always considered, so its unknown membership makes the day unknown. So is a
    campaign whose spend is unknown: it has no fact row for the day while the metrics dataset
    is not verified complete over it, so the caller passes ``spent`` as true.
    """
    count = len(series.times)
    if count == 0:
        return False
    first = max(bisect_right(series.times, lower) - 1, 0)
    last = min(bisect_left(series.times, upper), count - 1)
    for index in range(first, last + 1):
        if series.observations[index].budget_id != budget_id:
            continue
        instant = series.times[index]
        if spent or lower - gap <= instant <= upper + gap:
            return True
    return False


def _member_status_label(spans: Sequence[_Span]) -> str | None:
    """Summarise known, non-active member statuses: ``paused`` before ``ended``."""
    statuses = {span.value for span in spans if span.klass == "known"}
    for label in ("paused", "ended"):
        if label in statuses:
            return label
    return None


def _budget_campaigns(ctx: AnalysisContext) -> Mapping[int, tuple[int, ...]]:
    """Per Google budget, the campaigns that showed it in any observation, ascending.

    Built in one pass over the campaign entries of ``ctx.observations`` and memoised in
    ``ctx.cache``, so each shared budget visits only its own candidates rather than scanning
    every entity's observations once per budget.
    """
    key = "metrics.budget_campaigns"
    cached = ctx.cache.get(key)
    if isinstance(cached, Mapping):
        return cached
    shown: dict[int, set[int]] = defaultdict(set)
    for (entity_type, entity_id), observations in ctx.observations.items():
        if entity_type != "campaign":
            continue
        for budget_id in {obs.budget_id for obs in observations}:
            if budget_id is not None:
                shown[budget_id].add(entity_id)
    inverse = MappingProxyType({bid: tuple(sorted(cids)) for bid, cids in shown.items()})
    ctx.cache[key] = inverse
    return inverse


def _shared_candidate(ctx: AnalysisContext, campaign_id: int) -> _Candidate:
    """A campaign's dated membership and status spans over the window (memoised).

    Neither span map depends on the budget asked about: the window dates, the Google zone and
    ``budget.max_observation_gap_hours`` are fixed per context, and the comparison with a
    ``budget_id`` stays in :func:`shared_budget_history`'s day loop. A campaign that showed
    several shared budgets is therefore classified once for all of them.
    """
    key = f"metrics.shared_candidate:{campaign_id}"
    cached = ctx.cache.get(key)
    if isinstance(cached, _Candidate):
        return cached
    gap = timedelta(hours=_gate_config(ctx).max_gap_hours)
    tz = _source_tz(ctx, "google")
    dates = ctx.window.dates()
    observations = ctx.observations.get(("campaign", campaign_id), ())
    membership_series = _Series.of(observations, membership_value)
    candidate = _Candidate(
        series=membership_series,
        membership=MappingProxyType(_classify(membership_series, dates, tz, gap)),
        status=MappingProxyType(_classify(_Series.of(observations, status_value), dates, tz, gap)),
    )
    ctx.cache[key] = candidate
    return candidate


def shared_budget_history(ctx: AnalysisContext, budget_id: int) -> BudgetHistory:
    """Return a Google explicitly shared budget's history over the window (AAP 0.5.4.5).

    * **Dated membership**: campaign ``c`` is a member on day ``d`` when its campaign
      observations bracketing ``d`` all show this ``budget_id``, classified with
      :func:`membership_value`. Membership is evaluated day by day from snapshot history,
      never from today's member list, so a campaign that joined or left mid-window counts
      only on its member days.
    * **Budget-active day**: the budget's amount is whole-day known; the membership of every
      campaign that showed this ``budget_id`` in an observation bracketing ``d``
      (:func:`_shows_budget`) is whole-day known, so no campaign changed budgets during
      ``d``; and at least one member is whole-day known with status ``active``. Budget-active
      days replace known-budget active days in every gate.
    * **Aggregation**: the day's spend is the sum over that day's members only; ``u_d`` is
      that sum ÷ the shared amount, the only cap the members have. A member without a fact row
      counts 0 only while ``google.metrics_daily`` is ``complete`` over a covered range holding
      the date; otherwise the day is unobserved. A non-member without a fact row under no such
      coverage has unknown spend and is treated as having spent (:func:`_shows_budget`).
    * **Lost impression share** (the Google capped-day alternative): the spend-weighted mean
      of the members' ``search_budget_lost_impression_share`` that day, over members that
      spent and reported it. Spend weighting makes the share describe where the shared
      budget's money went; with no such member the clause does not apply.

    An unknown amount or membership makes the day ``unobserved`` (missing bracket or gap) or
    ``changed_intraday`` (a change during the day); with both known and no member known
    active, a member status that is itself unknown makes the day unknown the same way.

    ``latest_daily_budget`` is the current shared amount, not a historical one: the daily
    amount of the budget's newest observation at or before ``ctx.now``, from its window
    series or ``ctx.latest_observations``, whichever is newer; ``None`` when that observation
    shows no daily amount (:func:`_latest_daily`). Memoised in ``ctx.cache``.

    Raises:
        KeyError: When the budget is not in the context.
        ValueError: When the budget is not explicitly shared: a campaign's own budget is
            evaluated with :func:`campaign_budget_history`.
    """
    key = f"metrics.shared_history:{budget_id}"
    cached = ctx.cache.get(key)
    if isinstance(cached, BudgetHistory):
        return cached
    budget = ctx.budgets.get(budget_id)
    if budget is None:
        raise KeyError(f"budget {budget_id} is not in the analysis context")
    if not budget.explicitly_shared:
        raise ValueError(
            f"budget {budget_id} is not explicitly shared; its campaign is the budget subject"
        )
    config = _gate_config(ctx)
    gap = timedelta(hours=config.max_gap_hours)
    tz = _source_tz(ctx, "google")
    dates = ctx.window.dates()
    amount_series = _Series.of(ctx.observations.get(("budget", budget_id), ()), budget_amount_value)
    amount_spans = _classify(amount_series, dates, tz, gap)

    candidates: dict[int, _Candidate] = {
        campaign_id: _shared_candidate(ctx, campaign_id)
        for campaign_id in _budget_campaigns(ctx).get(budget_id, ())
    }

    applicable = budget.period == "DAILY"
    member_days: dict[int, list[date]] = defaultdict(list)
    days: list[BudgetDay] = []
    for day in dates:
        lower, upper = _day_bounds(day, tz)
        members: list[int] = []
        relevant: list[int] = []
        for campaign_id, candidate in candidates.items():
            membership = candidate.membership[day]
            if membership.klass == "known" and membership.value == budget_id:
                members.append(campaign_id)
                relevant.append(campaign_id)
                member_days[campaign_id].append(day)
                continue
            fact = _facts_by_date(ctx, campaign_id).get(day)
            spent = fact.cost > 0 if fact is not None else not _metrics_covered(ctx, "google", day)
            if _shows_budget(candidate.series, lower, upper, gap, budget_id, spent=spent):
                relevant.append(campaign_id)
        member_facts = [_facts_by_date(ctx, cid).get(day) for cid in members]
        shown_spend = sum((fact.cost for fact in member_facts if fact is not None), _ZERO)

        amount = amount_spans[day]
        membership_classes = {candidates[cid].membership[day].klass for cid in relevant}
        if amount.klass == "unobserved" or "unobserved" in membership_classes:
            days.append(_unknown_day(day, "unobserved", shown_spend))
            continue
        if amount.klass == "changed_intraday" or "changed_intraday" in membership_classes:
            days.append(_unknown_day(day, "changed_intraday", shown_spend))
            continue
        statuses = [candidates[cid].status[day] for cid in members]
        daily = _shared_daily(amount.value)
        if not any(span.klass == "known" and span.value == "active" for span in statuses):
            status_classes = {span.klass for span in statuses}
            if "unobserved" in status_classes:
                days.append(_unknown_day(day, "unobserved", shown_spend))
            elif "changed_intraday" in status_classes:
                days.append(_unknown_day(day, "changed_intraday", shown_spend))
            else:
                label = _member_status_label(statuses)
                days.append(_inactive_day(day, label, daily, shown_spend))
            continue
        if not applicable or daily is None or daily <= 0:
            days.append(_inactive_day(day, "active", daily, shown_spend))
            continue
        if any(fact is None for fact in member_facts) and not _metrics_covered(ctx, "google", day):
            days.append(_unknown_day(day, "unobserved", shown_spend))
            continue
        weighted = weight = _ZERO
        for fact in member_facts:
            if fact is not None and fact.cost > 0 and fact.budget_lost_is is not None:
                weighted += fact.budget_lost_is * fact.cost
                weight += fact.cost
        lost_is = weighted / weight if weight > 0 else None
        days.append(_active_day(day, "active", daily, shown_spend, lost_is, config))

    history = _summarise(
        ("budget", budget_id),
        shared=True,
        applicable=applicable,
        days=days,
        latest=_latest_daily(
            ctx, ("budget", budget_id), amount_series, budget_amount_value, _shared_daily
        ),
        members={cid: tuple(member) for cid, member in member_days.items()},
        observation_count=len(_span_indices(amount_spans.values())),
        config=config,
    )
    ctx.cache[key] = history
    return history


def budget_history(ctx: AnalysisContext, subject: SubjectKey) -> BudgetHistory:
    """Return the history of a budget subject: a campaign's own budget or a shared budget.

    Raises:
        KeyError: When the subject is not in the context.
        ValueError: For an unknown subject type, a shared-budget member campaign, or a
            budget that is not explicitly shared.
    """
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return campaign_budget_history(ctx, subject_id)
    if subject_type == "budget":
        return shared_budget_history(ctx, subject_id)
    raise ValueError(f"subject type must be 'campaign' or 'budget', found {subject_type!r}")


def budget_subjects(ctx: AnalysisContext) -> list[SubjectKey]:
    """Return every budget subject utilization applies to, campaigns first, by id.

    AAP 0.5.4.1 and 0.5.4.5: every own-budget campaign with a daily budget (Google ``DAILY``
    own budgets, LinkedIn campaigns with ``dailyBudget``) plus every Google explicitly shared
    ``DAILY`` budget. Lifetime and custom-period budgets ("n/a") and campaigns drawing from a
    shared budget (evaluated at budget level) are excluded.
    """
    subjects: list[SubjectKey] = [
        ("campaign", campaign.id)
        for campaign in sorted(ctx.campaigns.values(), key=lambda c: c.id)
        if _draws_from_shared(ctx, campaign) is None and _utilization_applicable(campaign)
    ]
    subjects.extend(
        ("budget", budget.id)
        for budget in sorted(ctx.budgets.values(), key=lambda b: b.id)
        if budget.explicitly_shared and budget.period == "DAILY"
    )
    return subjects


def subject_campaigns(ctx: AnalysisContext, subject: SubjectKey) -> tuple[int, ...]:
    """Return the campaigns a budget subject's spend comes from, ascending.

    A campaign subject is itself. A shared budget's campaigns are those with member days in
    the window plus those drawing from it now, which is where a spend increase would land.

    Raises:
        KeyError: When the subject is not in the context.
        ValueError: For an unknown subject type or a budget that is not explicitly shared.
    """
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return (_campaign(ctx, subject_id).id,)
    history = budget_history(ctx, subject)
    current = {c.id for c in ctx.campaigns.values() if c.budget_id == subject_id}
    return tuple(sorted(set(history.members) | current))


def subject_objective_groups(ctx: AnalysisContext, subject: SubjectKey) -> frozenset[str]:
    """Return the objective groups of a subject's campaigns (more than one: efficiency "n/a").

    AAP 0.5.4.5 "Mixed objective groups": when a shared budget's members span more than one
    objective group, efficiency is "n/a" and B1, B2 and B3 are withheld for that budget.
    Campaigns not in the context are skipped.
    """
    return frozenset(
        ctx.campaigns[cid].objective_group
        for cid in subject_campaigns(ctx, subject)
        if cid in ctx.campaigns
    )


def subject_totals(ctx: AnalysisContext, subject: SubjectKey) -> Totals | None:
    """Return a subject's window totals: a campaign's, or a shared budget's over member days.

    AAP 0.5.4.5: a shared budget's outcomes and cost per outcome are aggregated over member
    days only. Returns ``None`` when the members span more than one objective group or
    currency, because those outcomes are never summed ("n/a"). Within one group, outcomes
    are ``None`` when any counted day did not report them (:class:`Totals`).

    Raises:
        KeyError: When the subject is not in the context.
        ValueError: For an unknown subject type or a budget that is not explicitly shared.
    """
    subject_type, subject_id = subject
    if subject_type == "campaign":
        return window_totals(ctx, (subject_id,))
    history = budget_history(ctx, subject)
    parts = [
        (ctx.campaigns[cid], member)
        for cid, member in history.members.items()
        if cid in ctx.campaigns
    ]
    groups = {(c.source, c.objective_group, ctx.account_currency(c.id)) for c, _ in parts}
    if len(groups) > 1:
        return None
    return _totals(ctx, parts)


def subject_baseline(ctx: AnalysisContext, subject: SubjectKey) -> Decimal | None:
    """Return the baseline cost per outcome a subject is compared with (AAP 0.5.4.1).

    A campaign: its account's average for its objective group (:func:`baseline_cpo`). A
    shared budget: the same for its members' account and objective group; ``None`` when
    the members span accounts or objective groups, or the budget has no member days.
    """
    group = _baseline_group(ctx, subject)
    return None if group is None else baseline_cpo(ctx, *group)


def _baseline_group(ctx: AnalysisContext, subject: SubjectKey) -> tuple[int, str] | None:
    """Return the ``(account, objective group)`` of :func:`subject_baseline`, if exactly one."""
    campaign_ids = (
        subject_campaigns(ctx, subject)
        if subject[0] == "campaign"
        else tuple(budget_history(ctx, subject).members)
    )
    keys = {
        (ctx.campaigns[cid].account_id, ctx.campaigns[cid].objective_group)
        for cid in campaign_ids
        if cid in ctx.campaigns
    }
    if len(keys) != 1:
        return None
    return next(iter(keys))


# ---------------------------------------------------------------------------
# Pure half: history gates (AAP 0.5.4.1 table)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateResult:
    """The outcome of one history gate, with every check observed against required.

    Attributes:
        passed: Whether every check holds.
        shortfalls: One sentence per failed check, for example
            "spending days 3 of 5 required", for ``Withheld`` lines and evidence.
        counts: ``{check: (observed, required)}`` for every check, failed or not, so
            evidence can show "known-budget days 9 of 7" beside a shortfall.
    """

    passed: bool
    shortfalls: tuple[str, ...]
    counts: Mapping[str, tuple[int, int]]

    def describe(self) -> str:
        """The shortfalls joined for one ``Withheld`` reason; empty when the gate passed."""
        return "; ".join(self.shortfalls)


def _share_required(share: Decimal, days: int) -> int:
    """The least whole number of days meeting ``share`` of ``days``: ⌈share x days⌉."""
    return int((share * Decimal(days)).to_integral_value(rounding=ROUND_CEILING))


def _percent(share: Decimal) -> str:
    """Render a share as a percentage without exponent or trailing zeros: 0.70 → ``70``."""
    return format((share * 100).normalize(), "f")


def _gate(checks: Sequence[tuple[str, str, int, int, str]]) -> GateResult:
    """Evaluate ``(key, label, observed, required, note)`` checks into a :class:`GateResult`."""
    shortfalls: list[str] = []
    counts: dict[str, tuple[int, int]] = {}
    for key, label, observed, required, note in checks:
        counts[key] = (observed, required)
        if observed < required:
            suffix = f" ({note})" if note else ""
            shortfalls.append(f"{label} {observed} of {required} required{suffix}")
    return GateResult(not shortfalls, tuple(shortfalls), MappingProxyType(counts))


def _base_checks(
    history: BudgetHistory, config: GateConfig
) -> list[tuple[str, str, int, int, str]]:
    """The checks every gate starts with: known-budget days and spending days."""
    return [
        ("known_budget_days", history.known_label, history.known_active, config.min_known_days, ""),
        ("spending_days", "spending days", history.spending, config.min_spending_days, ""),
    ]


def _share_note(config: GateConfig, history: BudgetHistory) -> str:
    """``70% of 7 known-budget days``: how a share requirement was derived."""
    return f"{_percent(config.sustained_share)}% of {history.known_active} {history.known_label}"


def _resolve_config(history: BudgetHistory, thresholds: Thresholds | None) -> GateConfig:
    """The gate minimums to apply: ``thresholds`` when given, else the history's own."""
    return history.config if thresholds is None else GateConfig.from_thresholds(thresholds)


def cap_constrained(history: BudgetHistory, thresholds: Thresholds | None = None) -> GateResult:
    """The cap-constrained gate of C2 and B1 (AAP 0.5.4.1).

    Requires ``budget.min_known_days`` (7) known-budget active days, at least
    ``budget.min_spending_days`` (5) spending days, and capped days ≥ ``budget.min_capped_days``
    (5) and ≥ ``budget.sustained_share`` (70%) of the known-budget active days. One capped
    day followed by six idle days is therefore neither cap-constrained nor high-utilization.

    Args:
        history: The subject's history.
        thresholds: The configuration whose gate minimums apply; the history's own when
            omitted. The daily predicates were fixed when the history was built.
    """
    config = _resolve_config(history, thresholds)
    share = _share_required(config.sustained_share, history.known_active)
    checks = _base_checks(history, config)
    checks.append(("capped_days", "capped days", history.capped, config.min_capped_days, ""))
    checks.append(
        ("capped_share", "capped days", history.capped, share, _share_note(config, history))
    )
    return _gate(checks)


def high_utilization(history: BudgetHistory, thresholds: Thresholds | None = None) -> GateResult:
    """The high-utilization gate of B2 and G1 (AAP 0.5.4.1).

    Requires 7 known-budget active days; at least 5 spending days making up ≥ 70% of them;
    and high-utilization days (``u_d ≥ budget.high_utilization``) on ≥ 70% of them. High
    utilization on 4 of 7 days (57%) fails; on 5 of 7 (71%) it passes.

    Args:
        history: The subject's history.
        thresholds: The configuration whose gate minimums apply; the history's own when
            omitted.
    """
    config = _resolve_config(history, thresholds)
    share = _share_required(config.sustained_share, history.known_active)
    note = _share_note(config, history)
    checks = _base_checks(history, config)
    checks.append(("spending_share", "spending days", history.spending, share, note))
    checks.append(("high_utilization_days", "high-utilization days", history.high, share, note))
    return _gate(checks)


def under_utilized(history: BudgetHistory, thresholds: Thresholds | None = None) -> GateResult:
    """The under-utilized gate of B3 (AAP 0.5.4.1).

    Requires 7 known-budget active days, at least 5 spending days, and under-utilized days
    (``u_d ≤ budget.under_utilization``, known idle days included) on ≥ 70% of them. The
    spending-day minimum keeps a budget that never serves from being a reallocation donor:
    a subject with only idle days is idle, not under-utilized.

    Args:
        history: The subject's history.
        thresholds: The configuration whose gate minimums apply; the history's own when
            omitted.
    """
    config = _resolve_config(history, thresholds)
    share = _share_required(config.sustained_share, history.known_active)
    checks = _base_checks(history, config)
    checks.append(
        (
            "under_utilized_days",
            "under-utilized days",
            history.under,
            share,
            _share_note(config, history),
        )
    )
    return _gate(checks)


# ---------------------------------------------------------------------------
# Pure half: B2 and the G1 predicate (AAP 0.5.4.2, 0.5.4.5)
# ---------------------------------------------------------------------------

B2Branch = Literal["poor_efficiency", "zero_outcomes"]
"""Which poor-efficiency branch a B2 assessment met; P2 suppresses ``zero_outcomes``."""

B2_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "b2.poor_efficiency_ratio",
    "b2.min_expected_outcomes",
)
"""The ``b2.*`` keys B2 and the G1 member predicate apply, for ``evidence.thresholds``."""


@dataclass(frozen=True, slots=True)
class B2Assessment:
    """Whether a budget subject is high-utilization with poor efficiency (B2, G1).

    Attributes:
        subject: The budget subject.
        meets: The high-utilization gate passes and efficiency is poor.
        branch: ``poor_efficiency`` (cost/outcome ≥ ``b2.poor_efficiency_ratio`` x baseline)
            or ``zero_outcomes`` (0 outcomes with expected outcomes ≥
            ``b2.min_expected_outcomes``); ``None`` when efficiency is not poor or "n/a".
            Decided exactly from the subject's and the account's totals, never from the
            rounded ``cpo``, ``baseline`` and ``expected`` shown beside it.
        gate: The high-utilization gate (:func:`high_utilization`).
        spend: The subject's window spend (member days for a shared budget).
        outcomes: Its outcomes; ``None`` when unavailable or "n/a" (mixed objective groups).
        cpo: Its cost per outcome; ``None`` at 0 or unavailable outcomes.
        baseline: The baseline cost per outcome (:func:`subject_baseline`).
        expected: Expected outcomes at the baseline (:func:`expected_outcomes`).
    """

    subject: SubjectKey
    meets: bool
    branch: B2Branch | None
    gate: GateResult
    spend: Decimal
    outcomes: Decimal | None
    cpo: Decimal | None
    baseline: Decimal | None
    expected: Decimal | None


def _poor_branch(
    ctx: AnalysisContext, totals: Totals | None, account: Totals | None
) -> B2Branch | None:
    """Return which poor-efficiency branch ``totals`` meets against the baseline, if any.

    ``account`` holds the totals the baseline divides (:func:`_baseline_totals`). Both
    branches are decided exactly, by cross-multiplying those totals, never by comparing
    rounded quotients: with a baseline of 50 / 30, a spend of 5 is exactly 3 expected
    outcomes, which 5 ÷ 1.666…667 would read as 2.999…999 and miss.

    * ``poor_efficiency``: outcomes > 0 and spend ÷ outcomes ≥ ratio x account spend ÷
      account outcomes, that is spend x account outcomes ≥ ratio x account spend x outcomes.
    * ``zero_outcomes``: 0 outcomes and spend ÷ a positive baseline ≥ the minimum, that is
      spend x account outcomes ≥ minimum x account spend.

    Unavailable outcomes (``None``) on either side, or an account without outcomes, meet
    neither branch. The thresholds are ``b2.poor_efficiency_ratio`` (1.5) and
    ``b2.min_expected_outcomes`` (3). The configuration documents the latter for B2 and the
    G1 member predicate; it holds the same value as ``c1.min_expected_outcomes`` but is tuned
    independently.
    """
    if totals is None or totals.outcomes is None or account is None:
        return None
    if account.outcomes is None or account.outcomes <= 0:
        return None
    spend, outcomes = Fraction(totals.spend), Fraction(totals.outcomes)
    account_spend, account_outcomes = Fraction(account.spend), Fraction(account.outcomes)
    ratio = Fraction(ctx.thresholds.ratio("b2.poor_efficiency_ratio"))
    if outcomes > 0 and spend * account_outcomes >= ratio * account_spend * outcomes:
        return "poor_efficiency"
    minimum = Fraction(ctx.thresholds.ratio("b2.min_expected_outcomes"))
    if outcomes == 0 and account_spend > 0 and spend * account_outcomes >= minimum * account_spend:
        return "zero_outcomes"
    return None


def assess_b2(ctx: AnalysisContext, subject: SubjectKey) -> B2Assessment:
    """Assess B2 for a budget subject (AAP 0.5.4.5 B2 row; memoised in ``ctx.cache``).

    Meets when the high-utilization gate passes (at least 7 known-budget active days, at
    least 5 spending days making up ≥ 70% of them, and high-utilization days on ≥ 70% of
    them) **and** efficiency is poor: cost/outcome ≥ ``b2.poor_efficiency_ratio`` (1.5) x
    baseline, or 0 outcomes with expected outcomes ≥ ``b2.min_expected_outcomes`` (3). A
    shared budget is judged on its aggregate over member days; with members in more than one
    objective group its efficiency is "n/a" and it cannot meet B2 at aggregate level.

    Raises:
        KeyError: When the subject is not in the context.
        ValueError: For a shared-budget member campaign or a non-shared budget subject.
    """
    key = f"metrics.b2:{subject[0]}:{subject[1]}"
    cached = ctx.cache.get(key)
    if isinstance(cached, B2Assessment):
        return cached
    history = budget_history(ctx, subject)
    gate = high_utilization(history)
    totals = subject_totals(ctx, subject)
    group = None if totals is None else _baseline_group(ctx, subject)
    baseline = None if group is None else baseline_cpo(ctx, *group)
    branch = _poor_branch(ctx, totals, None if group is None else _baseline_totals(ctx, *group))
    spend = _ZERO if totals is None else totals.spend
    outcomes = None if totals is None else totals.outcomes
    assessment = B2Assessment(
        subject=subject,
        meets=gate.passed and branch is not None,
        branch=branch,
        gate=gate,
        spend=spend,
        outcomes=outcomes,
        cpo=cost_per_outcome(spend, outcomes),
        baseline=baseline,
        expected=expected_outcomes(spend, baseline),
    )
    ctx.cache[key] = assessment
    return assessment


def member_poor_efficiency(ctx: AnalysisContext, budget_id: int, campaign_id: int) -> bool:
    """Whether a shared-budget member's own efficiency over its member days is poor.

    AAP 0.5.4.5 "Member-level safety predicate (G1)": a member has no utilization of its own;
    it meets B2 within the shared budget when the budget passes the high-utilization gate
    (checked by :func:`spend_increase_blocked`) and this predicate holds, with the same two
    branches as B2 against the baseline of the member's account and objective group.
    A campaign with no member day in the window is not poor.

    Raises:
        KeyError: When the budget or campaign is not in the context.
        ValueError: When the budget is not explicitly shared.
    """
    history = shared_budget_history(ctx, budget_id)
    campaign = _campaign(ctx, campaign_id)
    member_days = history.members.get(campaign_id, ())
    if not member_days:
        return False
    totals = _totals(ctx, [(campaign, member_days)])
    account = _baseline_totals(ctx, campaign.account_id, campaign.objective_group)
    return _poor_branch(ctx, totals, account) is not None


def spend_increase_blocked(ctx: AnalysisContext) -> frozenset[SubjectKey]:
    """Return every campaign and budget no spend increase may target: the G1 set.

    AAP 0.5.4.2 G1 and 0.5.4.5. Blocked are:

    * every own-budget campaign meeting B2 (:func:`assess_b2`), together with its own Google
      budget row;
    * every shared budget whose aggregate meets B2, or that passes the high-utilization gate
      while any member is poor-efficiency over its member days
      (:func:`member_poor_efficiency`), together with all of its campaigns
      (:func:`subject_campaigns`): a member has no budget of its own, so an increase on any
      of them raises spend under the same cap.

    G1 in ``precedence.py``, C2 eligibility in ``campaigns.py`` and W1 in
    ``white_papers.py`` read this one predicate. Memoised in ``ctx.cache``.

    Threshold keys: :data:`G1_THRESHOLD_KEYS`, which a rule that selects or excludes spend
    targets with this predicate copies into ``evidence.thresholds``.
    """
    key = "metrics.spend_increase_blocked"
    cached = ctx.cache.get(key)
    if isinstance(cached, frozenset):
        return cached
    blocked: set[SubjectKey] = set()
    for subject in budget_subjects(ctx):
        assessment = assess_b2(ctx, subject)
        subject_type, subject_id = subject
        if subject_type == "campaign":
            if assessment.meets:
                blocked.add(subject)
                own_budget = ctx.campaigns[subject_id].budget_id
                if ctx.campaigns[subject_id].source == "google" and own_budget is not None:
                    blocked.add(("budget", own_budget))
            continue
        campaigns = subject_campaigns(ctx, subject)
        poor_member = assessment.gate.passed and any(
            member_poor_efficiency(ctx, subject_id, cid)
            for cid in campaigns
            if cid in ctx.campaigns
        )
        if assessment.meets or poor_member:
            blocked.add(subject)
            blocked.update(("campaign", cid) for cid in campaigns)
    result = frozenset(blocked)
    ctx.cache[key] = result
    return result


G1_THRESHOLD_KEYS: Final[tuple[str, ...]] = (*BUDGET_THRESHOLD_KEYS, *B2_THRESHOLD_KEYS)
"""Every key the G1 predicate (:func:`spend_increase_blocked`) applies, for ``evidence.thresholds``.

The ``budget.*`` keys classify each subject's days and gate high utilization
(:data:`BUDGET_THRESHOLD_KEYS`); the ``b2.*`` keys judge poor efficiency
(:data:`B2_THRESHOLD_KEYS`). One shared contract, so every rule that names or leaves out a
spend target by G1 records the same controls.
"""


# ---------------------------------------------------------------------------
# Pure half: the Google counting contract (AAP 0.5.4.5 B4)
# ---------------------------------------------------------------------------

ContractDay = Literal["last_click", "weighted", "unknown"]
"""How Google counted ``metrics.conversions`` on one native day (B4).

``last_click``: every conversion action's inclusion was whole-day known, and every included
action was whole-day known with ``GOOGLE_ADS_LAST_CLICK`` and a ``ONE_PER_CLICK`` or
``MANY_PER_CLICK`` counting type, so the day's conversions are unweighted event counts. An
action excluded all day constrains nothing, whatever its model or counting type.
``weighted``: some included action was data-driven or external that day (a retired rule-based
model counts, Google having upgraded it to data-driven), as an observation captured during the
day or the day's whole-day-known settings show. ``unknown``: otherwise, no conversion action
exists, or some action's inclusion, or an included action's settings, was not whole-day known,
or was known but missing or unrecognised, for example a null attribution model or counting
type ("conversion counting contract unknown").
"""

LAST_CLICK_MODEL: Final = "GOOGLE_ADS_LAST_CLICK"
"""The only attribution model under which Google conversions are whole, unweighted counts."""

_WEIGHTED_MODELS: Final[frozenset[str]] = frozenset(
    {
        "EXTERNAL",
        "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
        "GOOGLE_SEARCH_ATTRIBUTION_FIRST_CLICK",
        "GOOGLE_SEARCH_ATTRIBUTION_LINEAR",
        "GOOGLE_SEARCH_ATTRIBUTION_POSITION_BASED",
        "GOOGLE_SEARCH_ATTRIBUTION_TIME_DECAY",
    }
)
"""``AttributionModelEnum`` names that affirmatively weight conversions: data-driven, external
and the retired rule-based models, which Google upgraded to data-driven. ``UNKNOWN``,
``UNSPECIFIED``, a null model or any other name is unknown, never weighted."""

_COUNTING_TYPES: Final[frozenset[str]] = frozenset({"MANY_PER_CLICK", "ONE_PER_CLICK"})
"""``ConversionActionCountingTypeEnum`` names that count whole conversions; any other value,
null, ``UNKNOWN`` and ``UNSPECIFIED`` included, leaves a last-click action's count unverified."""


def _inclusion_value(obs: Observation) -> bool | None:
    """Whether a conversion action counts in ``metrics.conversions`` at one observation.

    ``None`` when the payload's ``include_in_conversions_metric`` is absent or not a ``bool``:
    the inclusion is then unavailable, never assumed either way.
    """
    value = obs.payload.get("include_in_conversions_metric")
    return value if isinstance(value, bool) else None


def _contract_value(obs: Observation) -> tuple[object, object, object]:
    """A conversion action's counting settings at one observation (Q-G8 snapshot payload)."""
    payload = obs.payload
    return (
        payload.get("include_in_conversions_metric"),
        payload.get("attribution_model"),
        payload.get("counting_type"),
    )


def _weighted(value: Hashable | None) -> bool:
    """Whether a :func:`_contract_value` includes the action under a weighted model.

    Affirmative only: the inclusion is exactly ``True`` and the model is one of
    :data:`_WEIGHTED_MODELS`. A missing or unrecognised model is never weighted evidence.
    """
    return (
        isinstance(value, tuple)
        and value[0] is True
        and isinstance(value[1], str)
        and value[1] in _WEIGHTED_MODELS
    )


def _weighted_during(series: _Series, lower: datetime, upper: datetime) -> bool:
    """Whether an observation captured during ``[lower, upper)`` shows weighted counting.

    A snapshot states what was true at its instant, so one taken inside the day establishes
    that some of the day's conversions were weighted, whether or not the day is whole-day
    known.
    """
    start, stop = bisect_left(series.times, lower), bisect_left(series.times, upper)
    return any(_weighted(series.values[index]) for index in range(start, stop))


def _action_day(inclusion: _Span, settings: _Span) -> ContractDay | None:
    """One conversion action's contract on one native day; ``None`` when it is excluded.

    ``inclusion`` classifies :func:`_inclusion_value` and ``settings`` the full
    :func:`_contract_value` over the same observations. An inclusion not whole-day known, or
    known but unavailable, is ``unknown``; known ``False`` proves the action excluded all day,
    so its model and counting type never matter. An included action needs whole-day-known
    settings: a weighted model is ``weighted``; :data:`LAST_CLICK_MODEL` with a counting type
    in :data:`_COUNTING_TYPES` is ``last_click``; anything else is ``unknown``.
    """
    if inclusion.klass != "known" or inclusion.value is None:
        return "unknown"
    if inclusion.value is False:
        return None
    if settings.klass != "known" or not isinstance(settings.value, tuple):
        return "unknown"
    if _weighted(settings.value):
        return "weighted"
    model, counting = settings.value[1], settings.value[2]
    if model == LAST_CLICK_MODEL and isinstance(counting, str) and counting in _COUNTING_TYPES:
        return "last_click"
    return "unknown"


def counting_contract(ctx: AnalysisContext) -> dict[date, ContractDay]:
    """Return the counting contract of every window day in the Google account's zone.

    AAP 0.5.4.5 B4: the Poisson rate test is valid only on unweighted event counts. Each
    conversion action is classified per day from its Q-G8 observations with
    :func:`classify_days` twice: its ``include_in_conversions_metric`` alone (a ``bool``, else
    unavailable), and its full ``(include_in_conversions_metric, attribution_model,
    counting_type)``. Absent values are unavailable, never defaults. Per action and day
    (:func:`_action_day`):

    * inclusion not whole-day known, or known but unavailable → ``unknown``: nothing shows
      whether the action counts in ``metrics.conversions`` (for example before the first
      Q-G8 snapshot, or an inclusion flipped during the day);
    * inclusion known ``false`` → excluded all day: its model and counting type, and any
      change in them, never matter;
    * inclusion known ``true`` → its full settings must be whole-day known (a model or
      counting-type change during the day unsettles it) and then give ``weighted`` for a
      data-driven, external or retired rule-based model; ``last_click`` for
      :data:`LAST_CLICK_MODEL` with ``ONE_PER_CLICK`` or ``MANY_PER_CLICK``, both of which
      keep whole counts; and ``unknown`` otherwise, for example a null, ``UNKNOWN`` or
      unrecognised model, or last click with a null or ``UNSPECIFIED`` counting type.

    Day ``d`` is then, in order:

    * ``weighted`` when an observation captured during ``d`` shows an action with
      ``include_in_conversions_metric = true`` under a weighted model, whole-day known or not:
      the snapshot establishes that the action was data-driven or external on ``d`` (AAP
      0.5.4.5 B4, "data-driven or external on any day"), so a one-day switch to data-driven in
      a zone the snapshot cadence does not align with is weighted, never merely unknown; or
      when any action is ``weighted`` for the day;
    * else ``unknown`` when any action is ``unknown``, or the context has no conversion action
      at all, because nothing verifies the contract;
    * else ``last_click``. When every action is whole-day known excluded, the verified
      included set is empty and the day is ``last_click``: only included actions are
      constrained.

    Integrality of the daily values is B4's additional check, never a substitute for this.
    Memoised in ``ctx.cache``; each call returns a fresh ``dict``.
    """
    key = "metrics.counting_contract"
    cached = ctx.cache.get(key)
    if not isinstance(cached, Mapping):
        dates = ctx.window.dates()
        tz = _source_tz(ctx, "google")
        gap = timedelta(hours=_gate_config(ctx).max_gap_hours)
        action_ids = sorted(
            set(ctx.conversion_actions)
            | {eid for (etype, eid) in ctx.observations if etype == "conversion_action"}
        )
        observed = [ctx.observations.get(("conversion_action", aid), ()) for aid in action_ids]
        series = [_Series.of(item, _contract_value) for item in observed]
        spans = [_classify(item, dates, tz, gap) for item in series]
        inclusions = [
            _classify(_Series.of(item, _inclusion_value), dates, tz, gap) for item in observed
        ]
        contract: dict[date, ContractDay] = {}
        for day in dates:
            lower, upper = _day_bounds(day, tz)
            if any(_weighted_during(item, lower, upper) for item in series):
                contract[day] = "weighted"
                continue
            verdicts = {
                _action_day(inclusion[day], settings[day])
                for inclusion, settings in zip(inclusions, spans, strict=True)
            }
            if "weighted" in verdicts:
                contract[day] = "weighted"
            elif not action_ids or "unknown" in verdicts:
                contract[day] = "unknown"
            else:
                contract[day] = "last_click"
        cached = MappingProxyType(contract)
        ctx.cache[key] = cached
    return dict(cached)
