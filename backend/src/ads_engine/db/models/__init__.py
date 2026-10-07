"""ORM models of the Ads Action Engine schema, registered on one shared ``MetaData``.

Importing this package imports all seven model modules, so every table is registered on
``ads_engine.db.base.Base.metadata``. Alembic's ``env.py``, ``alembic check`` and any
``Base.metadata`` consumer see the complete schema only once that has happened, because
SQLAlchemy registers a table when its model class is defined, and a string foreign-key
target (``"campaign.id"``, ``"source_dataset.id"`` and so on) resolves only once the table it
names is registered on the same metadata.

The seven modules, in dependency order:

* ``ops``: ``source_dataset``, ``dataset_freshness``, ``app_user``, ``source_connection``,
  ``sync_run`` and ``sync_request``, plus :data:`SOURCES` and the other dataset and
  connection enumerations.
* ``accounts``: ad accounts, campaign groups, campaigns, budgets, conversion actions, ad
  groups, keywords, creatives, lead forms and their versions, and the content-asset
  (white paper) registry.
* ``facts``: daily fact tables in each source's native day, and the preset-window
  LinkedIn demographic segments.
* ``snapshots``: the append-only ``entity_snapshot`` observations.
* ``crm``: LinkedIn leads, the per-CRM-dataset ``lead_match`` state, and HubSpot contacts,
  meetings, deals, associations and association fetches.
* ``decisions``: the decision log and its discrepancies.
* ``analysis``: analysis runs and requests, recommendations, findings, withheld patterns,
  suppression counts and feedback.

The package re-exports every model class and every enumeration constant named in each
module's ``__all__``, so callers may write either form::

    from ads_engine.db.models import Campaign, SOURCES
    from ads_engine.db.models.accounts import Campaign

Importing it has no side effects beyond defining the model classes: it creates no engine,
opens no connection and loads no settings. ``Base`` is deliberately not re-exported; import
it from :mod:`ads_engine.db.base`.
"""

# isort: off
# Deliberate dependency order, not alphabetical: ``ops`` defines ``source_dataset`` (the
# target of every ``dataset_id`` foreign key) and ``SOURCES``, which the later modules import.
from ads_engine.db.models.ops import (
    COMPLETENESS,
    CONNECTION_STATUSES,
    DATASETS,
    DATASET_ROLES,
    DATASET_STATES,
    MODES,
    SOURCES,
    SYNC_RUN_STATUSES,
    AppUser,
    DatasetFreshness,
    SourceConnection,
    SourceDataset,
    SyncRequest,
    SyncRun,
)
from ads_engine.db.models.accounts import (
    BUDGET_PERIODS,
    CAMPAIGN_STATUSES,
    AdAccount,
    AdGroup,
    Budget,
    Campaign,
    CampaignGroup,
    ContentAsset,
    ContentAssetCreative,
    ContentAssetForm,
    ConversionAction,
    Creative,
    Keyword,
    LeadForm,
    LeadFormVersion,
)
from ads_engine.db.models.facts import (
    SEARCH_TERM_STATUSES,
    SEGMENT_FACETS,
    FactCampaignDaily,
    FactCreativeDaily,
    FactKeywordDaily,
    FactSearchTermDaily,
    FactSegmentWindow,
)
from ads_engine.db.models.snapshots import (
    ENTITY_TYPES,
    EntitySnapshot,
)
from ads_engine.db.models.crm import (
    ASSOCIATION_FETCH_STATUSES,
    CONTACT_READ_STATUSES,
    CRM_OBJECT_TYPES,
    LEAD_MATCH_STATUSES,
    TARGET_READ_STATUSES,
    CrmAssociation,
    CrmAssociationFetch,
    CrmContact,
    CrmDeal,
    CrmMeeting,
    Lead,
    LeadMatch,
)
from ads_engine.db.models.decisions import (
    DECISION_ORIGINS,
    DECISION_SHAPES,
    DISCREPANCY_KINDS,
    DISCREPANCY_RESOLUTIONS,
    DISCREPANCY_STATES,
    SUBJECT_TYPES,
    Decision,
    Discrepancy,
)
from ads_engine.db.models.analysis import (
    DATA_STATUSES,
    FEEDBACK_KINDS,
    RECOMMENDATION_SECTIONS,
    RUN_KINDS,
    RUN_STATUSES,
    RUN_TRIGGERS,
    AnalysisRequest,
    AnalysisRun,
    Recommendation,
    RecommendationFeedback,
    RunFinding,
    RunSuppression,
    RunWithheld,
)

# isort: on

__all__: list[str] = [
    "ASSOCIATION_FETCH_STATUSES",
    "BUDGET_PERIODS",
    "CAMPAIGN_STATUSES",
    "COMPLETENESS",
    "CONNECTION_STATUSES",
    "CONTACT_READ_STATUSES",
    "CRM_OBJECT_TYPES",
    "DATASETS",
    "DATASET_ROLES",
    "DATASET_STATES",
    "DATA_STATUSES",
    "DECISION_ORIGINS",
    "DECISION_SHAPES",
    "DISCREPANCY_KINDS",
    "DISCREPANCY_RESOLUTIONS",
    "DISCREPANCY_STATES",
    "ENTITY_TYPES",
    "FEEDBACK_KINDS",
    "LEAD_MATCH_STATUSES",
    "MODES",
    "RECOMMENDATION_SECTIONS",
    "RUN_KINDS",
    "RUN_STATUSES",
    "RUN_TRIGGERS",
    "SEARCH_TERM_STATUSES",
    "SEGMENT_FACETS",
    "SOURCES",
    "SUBJECT_TYPES",
    "SYNC_RUN_STATUSES",
    "TARGET_READ_STATUSES",
    "AdAccount",
    "AdGroup",
    "AnalysisRequest",
    "AnalysisRun",
    "AppUser",
    "Budget",
    "Campaign",
    "CampaignGroup",
    "ContentAsset",
    "ContentAssetCreative",
    "ContentAssetForm",
    "ConversionAction",
    "Creative",
    "CrmAssociation",
    "CrmAssociationFetch",
    "CrmContact",
    "CrmDeal",
    "CrmMeeting",
    "DatasetFreshness",
    "Decision",
    "Discrepancy",
    "EntitySnapshot",
    "FactCampaignDaily",
    "FactCreativeDaily",
    "FactKeywordDaily",
    "FactSearchTermDaily",
    "FactSegmentWindow",
    "Keyword",
    "Lead",
    "LeadForm",
    "LeadFormVersion",
    "LeadMatch",
    "Recommendation",
    "RecommendationFeedback",
    "RunFinding",
    "RunSuppression",
    "RunWithheld",
    "SourceConnection",
    "SourceDataset",
    "SyncRequest",
    "SyncRun",
]
