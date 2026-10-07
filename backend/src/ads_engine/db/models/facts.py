"""Daily fact tables and the LinkedIn demographic segment windows.

AAP 0.5.2 assigns these tables to this module:

* ``fact_campaign_daily``: campaign performance per native day (Google Q-G3), with the
  impression-share signals the cap rules read.
* ``fact_creative_daily``: LinkedIn creative performance per UTC day (``adAnalytics`` with
  ``pivot=CREATIVE``), including the lead-gen funnel counts.
* ``fact_keyword_daily``: Google keyword performance and quality score per native day
  (Q-G5 ``keyword_view``).
* ``fact_search_term_daily``: the Google search terms report per native day (Q-G6
  ``search_term_view``), keyed by the keyword criterion each term matched.
* ``fact_segment_window``: LinkedIn demographic aggregates for one exact preset window.

**Daily facts, never window aggregates** (AAP 0.3.2). Daily rows are fetched incrementally
and restated: ``db/repositories.upsert_facts`` writes them with ``INSERT ... ON CONFLICT
(<__natural_key__>) DO UPDATE``, so a re-run sync is idempotent, a restated value overwrites
the earlier one, and ``fetched_at`` records when the stored value was last written. The
analysis derives any window from these rows. The one exception is ``fact_segment_window``:
LinkedIn approximates and privacy-thresholds demographic values, so they cannot be summed
from daily rows and are stored per exact window instead.

**Native-day policy** (AAP 0.5.4.1). Each daily row keeps its source's native date exactly as
reported: Google ``segments.date`` is a day in the customer account's time zone and LinkedIn
days are UTC. Dates are never relabelled or rebucketed, and no computation combines two
sources' daily rows into one daily series.

**Missing values** (AAP 0.11.1). ``None`` (``NULL``) means "unavailable", never zero, and the
rules treat it so. ``cost``, ``impressions`` and ``clicks`` are ``NOT NULL`` on the three
Google-fed daily tables (``fact_campaign_daily``, ``fact_keyword_daily`` and
``fact_search_term_daily``), because every Google row carries them; their other metrics are
nullable. On ``fact_creative_daily`` every metric is nullable, ``cost``, ``impressions`` and
``clicks`` included, because a LinkedIn analytics element may omit any metric (the
``ad_analytics`` contract); an omitted metric is stored as ``NULL``, never coerced to zero.
Every ``fact_segment_window`` metric is nullable for the same reason. A ``NULL`` also
satisfies each table's ``ck_<table>_non_negative`` check.

**Dataset-qualified entity references** (AAP 0.3.3: only ``lead_match`` links two datasets).
Every entity reference is a composite foreign key (``<entity>_id``, ``dataset_id``) to the
entity table's unique (``dataset_id``, ``id``), so the referenced entity must belong to the
row's own dataset. Only same-dataset lineage therefore enters a computation, and purging
another dataset can never delete the row. The entity column is listed first, so the naming
convention still names each constraint ``fk_<table>_<entity>_id_<entity table>``.

**Indexes** (AAP 0.2.1: "Daily facts are indexed by entity and date"). Each daily table has
an (entity, ``date``) index that serves window aggregation and, because it leads with the
entity column of the composite foreign key, the ``ON DELETE CASCADE`` from the entity
table; PostgreSQL does not index referencing columns by itself. The natural-key unique
constraint leads with ``dataset_id`` and serves the cascade from ``source_dataset``.

Every foreign key is ``ON DELETE CASCADE``, so purging a retired dataset is the single
``DELETE FROM source_dataset WHERE id = :retired`` (AAP 0.3.3). Foreign keys name their
target tables as strings; ``ads_engine.db.models`` imports ``accounts`` before this module,
so the targets are registered on ``Base.metadata`` before the schema is compiled.

``backend/migrations/versions/0001_initial_schema.py`` mirrors every column, type,
nullability, constraint and index name declared here, including the nullable ``cost``,
``impressions`` and ``clicks`` of ``fact_creative_daily``; ``alembic check`` must report no
drift.
"""

from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Final

# Every daily table has a column attribute named ``date``. Python 3.14 evaluates class
# annotations lazily in a scope that sees the class's own names, so a ``Mapped[date]``
# annotation could resolve to that attribute instead of the type. Day columns are therefore
# annotated with the ``NativeDate`` alias, ``datetime.date`` is never imported by name here,
# and SQLAlchemy is imported as a module and always written ``sa.<name>``.
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ads_engine.db.base import (
    Base,
    DatasetScopedMixin,
    IdPkMixin,
    NativeDate,
    check_in,
    non_negative,
)
from ads_engine.db.models.ops import SOURCES

__all__ = [
    "SEARCH_TERM_STATUSES",
    "SEGMENT_FACETS",
    "FactCampaignDaily",
    "FactCreativeDaily",
    "FactKeywordDaily",
    "FactSearchTermDaily",
    "FactSegmentWindow",
]

SEGMENT_FACETS: Final[tuple[str, ...]] = ("job_function", "seniority", "industry")
"""Demographic facets of ``fact_segment_window``, one per LinkedIn pivot.

``job_function`` comes from ``MEMBER_JOB_FUNCTION``, ``seniority`` from ``MEMBER_SENIORITY``
and ``industry`` from ``MEMBER_INDUSTRY`` (AAP 0.2.4, 0.3.3).
"""

SEARCH_TERM_STATUSES: Final[tuple[str, ...]] = (
    "ADDED",
    "EXCLUDED",
    "ADDED_EXCLUDED",
    "NONE",
    "UNKNOWN",
    "UNSPECIFIED",
)
"""Google ``search_term_view.status`` values, stored as returned.

K1 considers terms with status ``NONE`` or ``ADDED`` and skips ``EXCLUDED`` and
``ADDED_EXCLUDED``; K2 considers ``NONE`` only (AAP 0.5.4.3). ``UNKNOWN`` and
``UNSPECIFIED`` are the enum's own catch-all values, kept so a row carrying one is stored
rather than rejected at ingest.
"""


class FactCampaignDaily(IdPkMixin, DatasetScopedMixin, Base):
    """Performance of one campaign on one native day.

    AAP 0.5.2 (``fact_campaign_daily``). Google rows come from Q-G3, bounded by
    ``segments.date`` with no status filter, so spend by a campaign removed during the
    window still counts; a LinkedIn campaign rollup, where one is written, uses UTC days.
    The pause, scale, budget and weekday/weekend rules aggregate this table (C1, C2, B1 to
    B4). ``date`` is the source's native day and is never rebucketed (AAP 0.5.4.1).

    Attributes:
        campaign_id: The :class:`~ads_engine.db.models.accounts.Campaign` the row measures,
            which must belong to the row's own dataset: the composite foreign key
            (``campaign_id``, ``dataset_id``) references the campaign's unique
            (``dataset_id``, ``id``).
        date: The source's native day: Google in the account's time zone, LinkedIn in UTC.
        cost: Spend in the account's currency (Google ``cost_micros`` / 1,000,000).
        impressions: Impressions on the day.
        clicks: Clicks on the day.
        conversions: Google ``metrics.conversions``; a double that is fractional under
            data-driven attribution and may exceed clicks under ``MANY_PER_CLICK``, so it is
            never assumed to be an integer count. ``None`` when unavailable.
        conversions_value: Google ``metrics.conversions_value``; ``None`` when unavailable.
        budget_lost_is: Google ``search_budget_lost_impression_share`` as a fraction from 0
            to 1; a value of at least 0.10 marks a capped day (AAP 0.5.4.1). ``None`` when
            Google does not report it, for example for a non-search campaign.
        impression_share: Google ``search_impression_share`` as a fraction from 0 to 1;
            ``None`` when unavailable.
        fetched_at: When this row's values were last written by an upsert.
    """

    __tablename__ = "fact_campaign_daily"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "campaign_id", "date")

    campaign_id: Mapped[int]
    date: Mapped[NativeDate]
    cost: Mapped[Decimal]
    impressions: Mapped[int]
    clicks: Mapped[int]
    conversions: Mapped[Decimal | None]
    conversions_value: Mapped[Decimal | None]
    budget_lost_is: Mapped[Decimal | None]
    impression_share: Mapped[Decimal | None]
    fetched_at: Mapped[datetime]

    __table_args__ = (
        check_in("source", SOURCES),
        non_negative(
            "cost",
            "impressions",
            "clicks",
            "conversions",
            "conversions_value",
            "budget_lost_is",
            "impression_share",
        ),
        # Dataset-qualified: the campaign must belong to this row's dataset.
        sa.ForeignKeyConstraint(
            ["campaign_id", "dataset_id"],
            ["campaign.id", "campaign.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dataset_id", "campaign_id", "date"),
        sa.Index("ix_fact_campaign_daily_campaign_date", "campaign_id", "date"),
    )


class FactCreativeDaily(IdPkMixin, DatasetScopedMixin, Base):
    """Performance of one LinkedIn creative on one UTC day, including the lead-gen funnel.

    AAP 0.5.2 (``fact_creative_daily``). Rows come from ``adAnalytics`` with
    ``q=analytics``, ``pivot=CREATIVE`` and ``timeGranularity=DAILY``; LinkedIn reports daily
    buckets on UTC days and ``date`` keeps that day as returned (AAP 0.5.4.1). Spend is keyed
    by creative so white paper spend breaks down to the creative (AAP 0.5.4.7), and the
    funnel counts give the copy rules their stage rates: click rate = ``clicks`` /
    ``impressions``, open rate = ``form_opens`` / ``impressions`` and completion rate =
    ``leads`` / ``form_opens`` (CR1 to CR3, AAP 0.5.4.6).

    Every metric is nullable, because a LinkedIn analytics element may omit any metric (the
    ``ad_analytics`` contract): ``None`` means unavailable, never zero, and the rules treat
    it so.

    Attributes:
        creative_id: The :class:`~ads_engine.db.models.accounts.Creative` the row measures,
            which must belong to the row's own dataset: the composite foreign key
            (``creative_id``, ``dataset_id``) references the creative's unique
            (``dataset_id``, ``id``).
        date: The UTC day of LinkedIn's daily bucket.
        cost: ``costInLocalCurrency``, in the account's currency; ``None`` when the element
            omits it (unavailable, never 0).
        impressions: ``impressions``; ``None`` when the element omits it (unavailable,
            never 0).
        clicks: ``clicks``; ``None`` when the element omits it (unavailable, never 0).
        conversions: Not reported by LinkedIn's creative analytics, so ``None`` for LinkedIn
            rows; LinkedIn outcomes are ``leads`` and ``website_conversions``.
        conversions_value: ``None`` for LinkedIn rows, as for ``conversions``.
        form_opens: ``oneClickLeadFormOpens``; ``None`` when unavailable.
        leads: ``oneClickLeads``, the outcome of ``LEAD_GENERATION`` campaigns; ``None``
            when unavailable.
        landing_page_clicks: ``landingPageClicks``; ``None`` when unavailable.
        website_conversions: ``externalWebsiteConversions``, the outcome of other
            objectives; ``None`` when unavailable.
        fetched_at: When this row's values were last written by an upsert.
    """

    __tablename__ = "fact_creative_daily"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "creative_id", "date")

    creative_id: Mapped[int]
    date: Mapped[NativeDate]
    cost: Mapped[Decimal | None]
    impressions: Mapped[int | None]
    clicks: Mapped[int | None]
    conversions: Mapped[Decimal | None]
    conversions_value: Mapped[Decimal | None]
    form_opens: Mapped[int | None]
    leads: Mapped[int | None]
    landing_page_clicks: Mapped[int | None]
    website_conversions: Mapped[int | None]
    fetched_at: Mapped[datetime]

    __table_args__ = (
        check_in("source", SOURCES),
        non_negative(
            "cost",
            "impressions",
            "clicks",
            "conversions",
            "conversions_value",
            "form_opens",
            "leads",
            "landing_page_clicks",
            "website_conversions",
        ),
        # Dataset-qualified: the creative must belong to this row's dataset.
        sa.ForeignKeyConstraint(
            ["creative_id", "dataset_id"],
            ["creative.id", "creative.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dataset_id", "creative_id", "date"),
        sa.Index("ix_fact_creative_daily_creative_date", "creative_id", "date"),
    )


class FactKeywordDaily(IdPkMixin, DatasetScopedMixin, Base):
    """Performance and quality score of one Google keyword on one native day.

    AAP 0.5.2 (``fact_keyword_daily``). Rows come from Q-G5 (``keyword_view``), which
    returns keywords with traffic only; the current keyword list, including keywords with no
    traffic, lives in ``keyword`` and its quality-score snapshots. K3 compares
    ``historical_quality_score`` over the first and last seven days of the window and CPC
    and cost per conversion between the window's halves, and the search-term coverage
    finding divides search-term spend by keyword spend (AAP 0.5.4.3). ``date`` is the day in
    the Google account's time zone, as reported (AAP 0.5.4.1).

    Attributes:
        keyword_id: The :class:`~ads_engine.db.models.accounts.Keyword` the row measures,
            which must belong to the row's own dataset: the composite foreign key
            (``keyword_id``, ``dataset_id``) references the keyword's unique
            (``dataset_id``, ``id``).
        date: The Google native day (``segments.date``).
        cost: Spend in the account's currency (``cost_micros`` / 1,000,000).
        impressions: Impressions on the day.
        clicks: Clicks on the day.
        conversions: ``metrics.conversions``, possibly fractional; ``None`` when unavailable.
        conversions_value: ``metrics.conversions_value``; ``None`` when unavailable.
        historical_quality_score: ``metrics.historical_quality_score`` on the integer 1 to
            10 scale; ``None`` when Google has no score for the day.
        average_cpc: ``metrics.average_cpc`` in the account's currency (micros /
            1,000,000); ``None`` when unavailable, for example on a day with no clicks.
        fetched_at: When this row's values were last written by an upsert.
    """

    __tablename__ = "fact_keyword_daily"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "keyword_id", "date")

    keyword_id: Mapped[int]
    date: Mapped[NativeDate]
    cost: Mapped[Decimal]
    impressions: Mapped[int]
    clicks: Mapped[int]
    conversions: Mapped[Decimal | None]
    conversions_value: Mapped[Decimal | None]
    historical_quality_score: Mapped[int | None]
    average_cpc: Mapped[Decimal | None]
    fetched_at: Mapped[datetime]

    __table_args__ = (
        check_in("source", SOURCES),
        non_negative(
            "cost",
            "impressions",
            "clicks",
            "conversions",
            "conversions_value",
            "historical_quality_score",
            "average_cpc",
        ),
        # Dataset-qualified: the keyword must belong to this row's dataset.
        sa.ForeignKeyConstraint(
            ["keyword_id", "dataset_id"],
            ["keyword.id", "keyword.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dataset_id", "keyword_id", "date"),
        sa.Index("ix_fact_keyword_daily_keyword_date", "keyword_id", "date"),
    )


class FactSearchTermDaily(IdPkMixin, DatasetScopedMixin, Base):
    """One Google search term, as matched to one keyword criterion, on one native day.

    AAP 0.5.2 (``fact_search_term_daily``). Rows come from Q-G6 (``search_term_view``),
    bounded by ``segments.date`` with no status filter. The negative, expansion and cluster
    rules read this table (K1, K2, C3b, AAP 0.5.4.3 and 0.5.4.4). Google withholds
    low-volume terms for privacy, so search-term totals fall below keyword totals and the
    section shows the coverage share. ``date`` is the day in the Google account's time zone,
    as reported (AAP 0.5.4.1).

    The same term can match different keywords in one ad group on one day, so the natural
    key is (``dataset_id``, ``ad_group_id``, ``matched_criterion_id``, ``search_term``,
    ``date``). ``matched_criterion_id`` is part of that ``ON CONFLICT`` target and is
    therefore ``NOT NULL``: the parser stores the criterion id from
    ``segments.keyword.ad_group_criterion``, or ``''`` when Google returns no keyword
    segment, because a ``NULL`` never matches an ``ON CONFLICT`` key and every re-sync would
    insert a duplicate row.

    ``campaign_id`` is carried on the row, not derived through ``ad_group``, so campaign-level
    aggregation (K2's campaign cost per conversion, C3b) needs no join; its own index also
    serves the cascade when a campaign row is deleted.

    Both entity references are dataset-qualified: the composite foreign keys
    (``ad_group_id``, ``dataset_id``) and (``campaign_id``, ``dataset_id``) reference each
    entity's unique (``dataset_id``, ``id``), so the ad group and the campaign must belong to
    the row's own dataset.

    Attributes:
        ad_group_id: The :class:`~ads_engine.db.models.accounts.AdGroup` that served the
            term, of the row's own dataset.
        campaign_id: The :class:`~ads_engine.db.models.accounts.Campaign` of that ad group,
            of the row's own dataset.
        date: The Google native day (``segments.date``).
        search_term: ``search_term_view.search_term``, as the user typed it.
        term_status: ``search_term_view.status`` (:data:`SEARCH_TERM_STATUSES`).
        matched_criterion_id: The matched keyword's criterion id, or ``''`` when Google
            returns no keyword segment.
        matched_keyword_text: ``segments.keyword.info.text``; ``None`` when absent.
        matched_match_type: ``segments.keyword.info.match_type``, stored as returned;
            ``None`` when absent.
        cost: Spend in the account's currency (``cost_micros`` / 1,000,000).
        impressions: Impressions on the day.
        clicks: Clicks on the day.
        conversions: ``metrics.conversions``, possibly fractional; any value above zero
            counts as converting for K1. ``None`` when unavailable.
        conversions_value: ``metrics.conversions_value``; ``None`` when unavailable.
        fetched_at: When this row's values were last written by an upsert.
    """

    __tablename__ = "fact_search_term_daily"
    __natural_key__: ClassVar[tuple[str, ...]] = (
        "dataset_id",
        "ad_group_id",
        "matched_criterion_id",
        "search_term",
        "date",
    )

    ad_group_id: Mapped[int]
    campaign_id: Mapped[int]
    date: Mapped[NativeDate]
    search_term: Mapped[str]
    term_status: Mapped[str]
    matched_criterion_id: Mapped[str]
    matched_keyword_text: Mapped[str | None]
    matched_match_type: Mapped[str | None]
    cost: Mapped[Decimal]
    impressions: Mapped[int]
    clicks: Mapped[int]
    conversions: Mapped[Decimal | None]
    conversions_value: Mapped[Decimal | None]
    fetched_at: Mapped[datetime]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("term_status", SEARCH_TERM_STATUSES),
        non_negative("cost", "impressions", "clicks", "conversions", "conversions_value"),
        # Dataset-qualified: the ad group and the campaign must belong to this row's dataset.
        sa.ForeignKeyConstraint(
            ["ad_group_id", "dataset_id"],
            ["ad_group.id", "ad_group.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "dataset_id"],
            ["campaign.id", "campaign.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "dataset_id",
            "ad_group_id",
            "matched_criterion_id",
            "search_term",
            "date",
            name="uq_fact_search_term_daily_natural",
        ),
        sa.Index("ix_fact_search_term_daily_ad_group_date", "ad_group_id", "date"),
        sa.Index("ix_fact_search_term_daily_campaign_date", "campaign_id", "date"),
    )


class FactSegmentWindow(IdPkMixin, DatasetScopedMixin, Base):
    """LinkedIn demographic aggregates of one campaign over one exact preset window.

    AAP 0.5.2 (``fact_segment_window``) and 0.3.2 (fetch policy). LinkedIn approximates and
    privacy-thresholds demographic values, returns only the top 100 values per pivot and
    omits values with too few members, so these figures are never summed from daily rows.
    They are fetched with ``timeGranularity=ALL`` for each preset in ``window.presets``,
    every preset ending on the default window end, and stored keyed by that exact window.
    The launch rule C3a reads only a row set whose ``window_start`` and ``window_end`` equal
    the run's window; for any other window C3a is withheld with the finding
    ``segment_window_unavailable`` and never reuses another window's segments
    (AAP 0.5.4.4). Window dates are calendar dates on LinkedIn's UTC days.

    Every metric is nullable because a pivot may not return it: ``None`` means unavailable
    from that pivot, never zero, and C3a does not fire when the lead metric is unavailable.

    Attributes:
        campaign_id: The :class:`~ads_engine.db.models.accounts.Campaign` the segment
            describes, which must belong to the row's own dataset: the composite foreign key
            (``campaign_id``, ``dataset_id``) references the campaign's unique
            (``dataset_id``, ``id``).
        facet: ``job_function``, ``seniority`` or ``industry`` (:data:`SEGMENT_FACETS`).
        value: The pivot value as LinkedIn returns it, for example
            ``urn:li:seniority:5``.
        window_start: First day of the preset window, inclusive.
        window_end: Last day of the preset window, inclusive; never before
            ``window_start``.
        impressions: ``impressions`` for the segment; ``None`` when unavailable.
        clicks: ``clicks`` for the segment; ``None`` when unavailable.
        cost: ``costInLocalCurrency`` for the segment, in the account's currency; ``None``
            when unavailable.
        leads: ``oneClickLeads`` for the segment; ``None`` when unavailable.
        fetched_at: When this row's values were last written by an upsert.
    """

    __tablename__ = "fact_segment_window"
    __natural_key__: ClassVar[tuple[str, ...]] = (
        "dataset_id",
        "campaign_id",
        "facet",
        "value",
        "window_start",
        "window_end",
    )

    # The natural key leads with dataset_id, so the cascade from campaign needs this index.
    campaign_id: Mapped[int] = mapped_column(index=True)
    facet: Mapped[str]
    value: Mapped[str]
    window_start: Mapped[NativeDate]
    window_end: Mapped[NativeDate]
    impressions: Mapped[int | None]
    clicks: Mapped[int | None]
    cost: Mapped[Decimal | None]
    leads: Mapped[int | None]
    fetched_at: Mapped[datetime]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("facet", SEGMENT_FACETS),
        non_negative("impressions", "clicks", "cost", "leads"),
        sa.CheckConstraint("window_end >= window_start", name="window_order"),
        # Dataset-qualified: the campaign must belong to this row's dataset.
        sa.ForeignKeyConstraint(
            ["campaign_id", "dataset_id"],
            ["campaign.id", "campaign.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "dataset_id",
            "campaign_id",
            "facet",
            "value",
            "window_start",
            "window_end",
            name="uq_fact_segment_window_natural",
        ),
    )
