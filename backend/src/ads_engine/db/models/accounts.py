"""Platform entity tables: accounts, campaigns, budgets, keywords, creatives and lead forms.

AAP 0.5.2 assigns these tables to this module:

* ``ad_account``: one Google customer or LinkedIn ad account, with its currency and, for
  Google, the time zone that sets Google's native day.
* ``campaign_group``: LinkedIn campaign groups, shown in campaign evidence.
* ``campaign``: campaigns of both sources, with the status normalised to
  :data:`CAMPAIGN_STATUSES` and the raw platform status kept beside it.
* ``budget``: Google ``campaign_budget`` rows; an explicitly shared budget is a decision
  subject in its own right.
* ``conversion_action``: Google conversion actions (Q-G8), the counting contract of B4.
* ``ad_group`` and ``keyword``: the Google ad groups and current keyword list (Q-G4, Q-G7).
* ``creative``, ``lead_form`` and ``lead_form_version``: LinkedIn creatives, lead forms by
  canonical URN and the per-version email question map.
* ``content_asset``, ``content_asset_form`` and ``content_asset_creative``: the white paper
  registry loaded from ``config/content_assets.yaml``.

Every source-derived table mixes in :class:`~ads_engine.db.base.DatasetScopedMixin`, so its
rows carry ``source`` and a ``dataset_id`` that cascades from ``source_dataset``. Every
foreign key between two rows of the same dataset is ``ON DELETE CASCADE`` as well, so purging
a retired dataset is the single ``DELETE FROM source_dataset WHERE id = :retired`` (AAP 0.3.3)
and PostgreSQL resolves the order itself. Each such foreign-key column is indexed unless it
already leads an index, because PostgreSQL does not index referencing columns and the
cascade would otherwise scan the child table once per deleted parent row.

Normalised enumerations are ``text`` guarded by :func:`~ads_engine.db.base.check_in`. Raw
platform values (``campaign.raw_status``, ``campaign_group.status``, ``ad_group.status``,
``keyword.status`` and ``match_type``, ``creative.intended_status``, and
``conversion_action.status``, ``counting_type`` and ``attribution_model``) are stored exactly
as the platform returns them, with no ``CHECK``, so a value a platform adds later is kept
rather than rejected at ingest.

External ids are ``text``: Google INT64 ids arrive as decimal strings and LinkedIn ids are
URNs. Each dataset-scoped model declares ``__natural_key__``, the columns of its natural-key
unique constraint, which ``db/repositories.py`` uses as the ``ON CONFLICT`` target of its
idempotent upserts.

``backend/migrations/versions/0001_initial_schema.py`` mirrors every column, type,
nullability, server default, constraint and index name declared here; ``alembic check``
must report no drift.
"""

from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Final

# Imported as a module and always written ``sa.<name>``: ``Keyword`` has a column attribute
# named ``text``, which a bare ``from sqlalchemy import text`` would make easy to confuse.
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ads_engine.db.base import Base, DatasetScopedMixin, IdPkMixin, check_in
from ads_engine.db.models.ops import SOURCES

__all__ = [
    "BUDGET_PERIODS",
    "CAMPAIGN_STATUSES",
    "AdAccount",
    "AdGroup",
    "Budget",
    "Campaign",
    "CampaignGroup",
    "ContentAsset",
    "ContentAssetCreative",
    "ContentAssetForm",
    "ConversionAction",
    "Creative",
    "Keyword",
    "LeadForm",
    "LeadFormVersion",
]

CAMPAIGN_STATUSES: Final[tuple[str, ...]] = ("active", "paused", "ended")
"""Normalised campaign status, mapped from each source's raw status by the parsers.

Decisions record the same values, so the discrepancy check (C0) compares like with like.
"""

BUDGET_PERIODS: Final[tuple[str, ...]] = ("DAILY", "CUSTOM_PERIOD")
"""Google ``campaign_budget.period``: a ``DAILY`` budget has utilization; a custom-period one
is a lifetime budget, shown as "n/a" and excluded from cap rules (AAP 0.5.4.1)."""


class AdAccount(IdPkMixin, DatasetScopedMixin, Base):
    """One ad account of a source: a Google customer or a LinkedIn sponsored account.

    AAP 0.5.2 (``ad_account``). Amounts are shown in each account's currency and are never
    converted or summed across currencies, so every comparison and total in the analysis is
    taken within one ``currency``. The Google account's ``timezone`` sets Google's native
    day boundary; LinkedIn days are UTC (AAP 0.5.4.1).

    Attributes:
        external_id: The Google customer id (10 digits) or the LinkedIn account id.
        name: The account's descriptive name.
        currency: ISO 4217 currency code of every amount in the account.
        timezone: IANA time zone of the account (Google ``customer.time_zone``); ``None``
            where the source reports none.
    """

    __tablename__ = "ad_account"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "source", "external_id")

    external_id: Mapped[str]
    name: Mapped[str]
    currency: Mapped[str]
    timezone: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "source", "external_id"),
    )


class CampaignGroup(IdPkMixin, DatasetScopedMixin, Base):
    """A LinkedIn campaign group, to which campaign budgets and status roll up.

    AAP 0.5.2 (``campaign_group``). Synced in every status on each entity sync and
    snapshotted as ``entity_type=campaign_group``; shown in campaign evidence. Groups are not
    decision subjects: decisions and discrepancies cover campaigns and budgets only.

    Attributes:
        account_id: The owning :class:`AdAccount`.
        external_id: The LinkedIn campaign group id.
        name: The group's name.
        status: The raw LinkedIn status, stored as returned.
        daily_budget: The group's daily budget, if it has one.
        total_budget: The group's total budget, if it has one.
        run_start: Start of the group's run schedule, if set.
        run_end: End of the group's run schedule, if set.
    """

    __tablename__ = "campaign_group"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "account_id", "external_id")

    account_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ad_account.id", ondelete="CASCADE"),
        index=True,
    )
    external_id: Mapped[str]
    name: Mapped[str]
    status: Mapped[str]
    daily_budget: Mapped[Decimal | None]
    total_budget: Mapped[Decimal | None]
    run_start: Mapped[datetime | None]
    run_end: Mapped[datetime | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "account_id", "external_id"),
    )


class Campaign(IdPkMixin, DatasetScopedMixin, Base):
    """A campaign of either source: the main subject of decisions and discrepancies.

    AAP 0.5.2 (``campaign``). ``status`` is normalised to :data:`CAMPAIGN_STATUSES` through a
    per-source map and ``raw_status`` keeps the platform's own value. A LinkedIn campaign
    carries its budgets on this row; a Google campaign references the ``campaign_budget`` it
    draws from through ``budget_id`` and repeats that budget's amount in ``daily_budget`` or
    ``total_budget`` according to ``budget_period``. Whether the budget is explicitly shared
    fixes the campaign's decision shape (``own_budget`` or ``shared_member``, AAP 0.5.2).

    The unique (``dataset_id``, ``id``) key is the target of the dataset-qualified foreign
    keys of ``fact_campaign_daily``, ``fact_search_term_daily``, ``fact_segment_window`` and
    ``lead``, so a row can reference only a campaign of its own dataset.

    Attributes:
        account_id: The owning :class:`AdAccount`.
        external_id: The Google campaign id or LinkedIn campaign id.
        name: The campaign's name.
        status: ``active``, ``paused`` or ``ended`` (:data:`CAMPAIGN_STATUSES`).
        raw_status: The platform status as returned, for example ``ENABLED`` or ``ARCHIVED``.
        objective_or_channel: LinkedIn ``objectiveType`` or Google
            ``advertising_channel_type``; selects the outcome metric (AAP 0.5.4.1).
        campaign_group_id: The LinkedIn :class:`CampaignGroup`; ``None`` for Google.
        budget_id: The Google :class:`Budget` the campaign draws from; ``None`` for LinkedIn.
        daily_budget: The campaign's daily budget, if it has one.
        total_budget: The campaign's lifetime or custom-period budget, if it has one.
        budget_period: ``DAILY`` or ``CUSTOM_PERIOD`` (:data:`BUDGET_PERIODS`) for Google;
            ``None`` for LinkedIn, whose budget fields say which budgets exist.
    """

    __tablename__ = "campaign"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "account_id", "external_id")

    account_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ad_account.id", ondelete="CASCADE"),
        index=True,
    )
    external_id: Mapped[str]
    name: Mapped[str]
    status: Mapped[str]
    raw_status: Mapped[str]
    objective_or_channel: Mapped[str | None]
    campaign_group_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("campaign_group.id", ondelete="CASCADE"),
        index=True,
    )
    budget_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("budget.id", ondelete="CASCADE"),
        index=True,
    )
    daily_budget: Mapped[Decimal | None]
    total_budget: Mapped[Decimal | None]
    budget_period: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("status", CAMPAIGN_STATUSES),
        check_in("budget_period", BUDGET_PERIODS),
        sa.UniqueConstraint("dataset_id", "account_id", "external_id"),
        # Anchors the dataset-qualified (campaign_id, dataset_id) foreign keys of child rows.
        sa.UniqueConstraint("dataset_id", "id"),
    )


class Budget(IdPkMixin, DatasetScopedMixin, Base):
    """A Google ``campaign_budget``, read with the campaigns that draw from it (Q-G2).

    AAP 0.5.2 (``budget``). A budget with ``explicitly_shared`` is a decision subject in its
    own right, evaluated at budget level over its dated membership (AAP 0.5.4.5); its member
    campaigns have the ``shared_member`` decision shape and never own an amount. LinkedIn
    budgets live on the :class:`Campaign` row, so this table holds Google budgets only.

    ``amount_micros`` keeps Google's native integer precision, so the discrepancy check (C0)
    compares the decided amount converted exactly to micros without rounding
    (AAP 0.5.4.4). ``amount`` is the same value in the account's currency.

    Attributes:
        external_id: The Google ``campaign_budget.id``.
        amount: The budget amount in the account's currency.
        amount_micros: The budget amount in micros, as Google reports it.
        period: ``DAILY`` or ``CUSTOM_PERIOD`` (:data:`BUDGET_PERIODS`).
        explicitly_shared: Whether the budget is shared across campaigns (server default
            ``false``).
    """

    __tablename__ = "budget"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "external_id")

    external_id: Mapped[str]
    amount: Mapped[Decimal]
    amount_micros: Mapped[int]
    period: Mapped[str]
    explicitly_shared: Mapped[bool] = mapped_column(server_default=sa.text("false"))

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("period", BUDGET_PERIODS),
        sa.UniqueConstraint("dataset_id", "external_id"),
    )


class ConversionAction(IdPkMixin, DatasetScopedMixin, Base):
    """A Google conversion action and the settings that say how Google counts it (Q-G8).

    AAP 0.5.2 (``conversion_action``). Read in every status, because a removed or hidden
    action may still have contributed to past days, and snapshotted on every entity sync as
    ``entity_type=conversion_action``. The B4 weekday/weekend rule reads the snapshots: the
    rate test runs only when every action included in ``metrics.conversions`` is whole-day
    known as last-click on every window day (AAP 0.5.4.5).

    Attributes:
        external_id: The Google ``conversion_action.id``.
        name: The action's name.
        status: The raw Google status, stored as returned.
        include_in_conversions_metric: Whether the action counts toward
            ``metrics.conversions`` (server default ``false``).
        counting_type: The raw counting type, for example ``ONE_PER_CLICK`` or
            ``MANY_PER_CLICK``.
        attribution_model: The raw attribution model, for example
            ``GOOGLE_ADS_LAST_CLICK``; ``None`` when Google returns none.
    """

    __tablename__ = "conversion_action"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "external_id")

    external_id: Mapped[str]
    name: Mapped[str]
    status: Mapped[str]
    include_in_conversions_metric: Mapped[bool] = mapped_column(server_default=sa.text("false"))
    counting_type: Mapped[str]
    attribution_model: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "external_id"),
    )


class AdGroup(IdPkMixin, DatasetScopedMixin, Base):
    """A Google ad group, enumerated by keyset scan on ``ad_group.id`` (Q-G4).

    AAP 0.5.2 (``ad_group``). Ad groups give keywords and search terms their campaign
    context, and their names take part in the C3b "keyword cluster without a campaign"
    check (AAP 0.5.4.4).

    The unique (``dataset_id``, ``id``) key is the target of the dataset-qualified foreign
    key of ``fact_search_term_daily``, so a search term can reference only an ad group of
    its own dataset.

    Attributes:
        campaign_id: The owning :class:`Campaign`.
        external_id: The Google ``ad_group.id``.
        name: The ad group's name.
        status: The raw Google status, stored as returned.
    """

    __tablename__ = "ad_group"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "campaign_id", "external_id")

    campaign_id: Mapped[int] = mapped_column(
        sa.ForeignKey("campaign.id", ondelete="CASCADE"),
        index=True,
    )
    external_id: Mapped[str]
    name: Mapped[str]
    status: Mapped[str]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "campaign_id", "external_id"),
        # Anchors the dataset-qualified (ad_group_id, dataset_id) foreign keys of child rows.
        sa.UniqueConstraint("dataset_id", "id"),
    )


class Keyword(IdPkMixin, DatasetScopedMixin, Base):
    """A Google keyword criterion from the current keyword list (Q-G7).

    AAP 0.5.2 (``keyword``). Every entity sync writes one row per non-removed keyword,
    including keywords with no traffic, and one quality-score snapshot per keyword, so K3
    can fall back to snapshots when daily scores are absent (AAP 0.5.4.3). Google's
    ``criterion_id`` is unique only within an ad group, so the natural key includes
    ``ad_group_id``.

    The unique (``dataset_id``, ``id``) key is the target of the dataset-qualified foreign
    key of ``fact_keyword_daily``, so a keyword fact can reference only a keyword of its own
    dataset.

    Attributes:
        ad_group_id: The owning :class:`AdGroup`.
        criterion_id: The Google ``ad_group_criterion.criterion_id``.
        text: The keyword text.
        match_type: The raw match type, for example ``EXACT`` or ``PHRASE``.
        status: The raw Google status, stored as returned.
        quality_score: The current ``quality_info.quality_score`` (1 to 10); ``None`` when
            Google leaves it unpopulated.
    """

    __tablename__ = "keyword"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "ad_group_id", "criterion_id")

    ad_group_id: Mapped[int] = mapped_column(
        sa.ForeignKey("ad_group.id", ondelete="CASCADE"),
        index=True,
    )
    criterion_id: Mapped[str]
    text: Mapped[str]
    match_type: Mapped[str]
    status: Mapped[str]
    quality_score: Mapped[int | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "ad_group_id", "criterion_id"),
        # Anchors the dataset-qualified (keyword_id, dataset_id) foreign keys of child rows.
        sa.UniqueConstraint("dataset_id", "id"),
    )


class Creative(IdPkMixin, DatasetScopedMixin, Base):
    """A LinkedIn creative: the unit spend, leads and white paper credit are traced to.

    AAP 0.5.2 (``creative``). Daily spend is keyed by the creative URN and each lead names
    the creative that produced it, so white paper rows break down to the creative
    (AAP 0.5.4.7). ``lead_form_urn`` is the canonical form URN of the lead-gen call-to-action
    destination, which resolves the creative's spend to a content asset unless a creative
    override says otherwise. Headline and intro text come from the referenced post.

    The unique (``dataset_id``, ``id``) key is the target of the dataset-qualified foreign
    keys of ``fact_creative_daily`` and ``lead``, so a row can reference only a creative of
    its own dataset.

    Attributes:
        campaign_id: The owning :class:`Campaign`.
        urn: The creative URN, for example ``urn:li:sponsoredCreative:123``.
        intended_status: The raw LinkedIn ``intendedStatus``, stored as returned.
        post_urn: The URN of the post the creative references, if any.
        headline: The post headline, if any.
        intro_text: The post intro text, if any.
        cta_label: The call-to-action label, if any.
        lead_form_urn: Canonical ``urn:li:leadGenForm:{id}`` of the call-to-action
            destination; ``None`` for creatives without a lead-gen form.
    """

    __tablename__ = "creative"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "urn")

    campaign_id: Mapped[int] = mapped_column(
        sa.ForeignKey("campaign.id", ondelete="CASCADE"),
        index=True,
    )
    urn: Mapped[str]
    intended_status: Mapped[str | None]
    post_urn: Mapped[str | None]
    headline: Mapped[str | None]
    intro_text: Mapped[str | None]
    cta_label: Mapped[str | None]
    lead_form_urn: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "urn"),
        # Anchors the dataset-qualified (creative_id, dataset_id) foreign keys of child rows.
        sa.UniqueConstraint("dataset_id", "id"),
    )


class LeadForm(IdPkMixin, DatasetScopedMixin, Base):
    """A LinkedIn lead-gen form, identified by its canonical URN across all its versions.

    AAP 0.5.2 (``lead_form``) and 0.3.3 (form identity). The content-asset registry and
    creative call-to-action destinations join on ``canonical_urn``, so every version of a
    form maps to the same asset. A row may first be created from a lead response's form
    decoration, before the ``leadForms`` sync has read the form's name and current version.

    Attributes:
        canonical_urn: The canonical ``urn:li:leadGenForm:{id}``.
        name: The form's name; ``None`` until known.
        current_version: The form's current ``versionId``; ``None`` until known.
    """

    __tablename__ = "lead_form"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "canonical_urn")

    canonical_urn: Mapped[str]
    name: Mapped[str | None]
    current_version: Mapped[int | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "canonical_urn"),
    )


class LeadFormVersion(IdPkMixin, DatasetScopedMixin, Base):
    """The email question map of one version of a LinkedIn lead form.

    AAP 0.5.2 (``lead_form_version``) and 0.3.3 (email decoding). Editing a form creates a new
    version and a question id can change meaning between versions, so emails are decoded per
    version: a response's version comes only from its ``versionedLeadGenFormUrn``, and its
    answers are read through that version's ``email_question_ids``. Rows are filled from each
    response's form decoration and from the ``leadForms`` sync of each form's current version.

    The natural key is (``lead_form_id``, ``version``), exactly as AAP 0.5.2 states; it is
    dataset-scoped through ``lead_form_id``, whose form belongs to one dataset. That unique
    index leads with ``lead_form_id``, so the cascade from :class:`LeadForm` needs no
    separate index. Because the natural key does not lead with ``dataset_id``, the direct
    ``dataset_id`` cascade from ``source_dataset`` is served by its own index,
    ``ix_lead_form_version_dataset_id``.

    Attributes:
        lead_form_id: The :class:`LeadForm` this version belongs to.
        version: The integer form version.
        email_question_ids: Ids of the questions whose ``predefinedField`` is an email field
            (server default ``'{}'::text[]``).
        first_seen_at: When this version was first observed.
    """

    __tablename__ = "lead_form_version"
    __natural_key__: ClassVar[tuple[str, ...]] = ("lead_form_id", "version")

    lead_form_id: Mapped[int] = mapped_column(
        sa.ForeignKey("lead_form.id", ondelete="CASCADE"),
    )
    version: Mapped[int]
    email_question_ids: Mapped[list[str]] = mapped_column(server_default=sa.text("'{}'::text[]"))
    first_seen_at: Mapped[datetime]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("lead_form_id", "version"),
        # The natural key leads with lead_form_id; this serves the purge's dataset_id cascade.
        sa.Index("ix_lead_form_version_dataset_id", "dataset_id"),
    )


class ContentAsset(IdPkMixin, Base):
    """A white paper (content asset) from the registry in ``config/content_assets.yaml``.

    AAP 0.5.2 (``content_asset``) and 0.5.4.7 (identification). The ad platforms do not
    identify content assets, so the registry maps each asset to its LinkedIn lead forms and
    optional creative overrides. The registry is configuration, not source data: these three
    tables carry no ``source`` or ``dataset_id`` and reference no dataset rows, so they
    survive every purge. ``db/repositories.sync_content_assets`` rewrites them from the YAML
    on :attr:`__natural_key__`.

    Attributes:
        slug: The asset's stable identifier in the registry, unique.
        title: The asset's display title.
    """

    __tablename__ = "content_asset"
    __natural_key__: ClassVar[tuple[str, ...]] = ("slug",)

    slug: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]


class ContentAssetForm(IdPkMixin, Base):
    """The mapping of one LinkedIn lead form, by canonical URN, to its content asset.

    AAP 0.5.2 (``content_asset_form``) and 0.5.4.7 (asset resolution). Joins use the
    canonical URN string, never a ``lead_form`` row id, so every version of a form, in every
    dataset, maps to the same asset. ``lead_form_urn`` is unique: a form listed under two
    assets fails config load. Loaded from ``config/content_assets.yaml`` by
    ``db/repositories.sync_content_assets``.

    Attributes:
        asset_id: The :class:`ContentAsset` the form gates.
        lead_form_urn: The canonical ``urn:li:leadGenForm:{id}``, unique.
    """

    __tablename__ = "content_asset_form"
    __natural_key__: ClassVar[tuple[str, ...]] = ("lead_form_urn",)

    # The registry tables hold a handful of rows and are rewritten whole from YAML, so the
    # cascade from content_asset needs no index on asset_id.
    asset_id: Mapped[int] = mapped_column(
        sa.ForeignKey("content_asset.id", ondelete="CASCADE"),
    )
    lead_form_urn: Mapped[str] = mapped_column(unique=True)


class ContentAssetCreative(IdPkMixin, Base):
    """An optional override that assigns one creative, by URN, to a content asset.

    AAP 0.5.2 (``content_asset_creative``) and 0.5.4.7 (asset resolution). The override takes
    precedence over the creative's call-to-action form for both spend and leads, and serves
    creatives without a lead-gen form, such as landing-page creatives. ``creative_urn`` is
    unique: a creative listed under two assets, or an override that contradicts the
    creative's own form mapping, fails config load. Loaded from ``config/content_assets.yaml``
    by ``db/repositories.sync_content_assets``.

    Attributes:
        asset_id: The :class:`ContentAsset` the creative is credited to.
        creative_urn: The creative URN, unique.
    """

    __tablename__ = "content_asset_creative"
    __natural_key__: ClassVar[tuple[str, ...]] = ("creative_urn",)

    asset_id: Mapped[int] = mapped_column(
        sa.ForeignKey("content_asset.id", ondelete="CASCADE"),
    )
    creative_urn: Mapped[str] = mapped_column(unique=True)
