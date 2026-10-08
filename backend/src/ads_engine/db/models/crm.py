"""LinkedIn leads, durable lead-to-contact matching and the HubSpot CRM objects they reach.

AAP 0.5.2 assigns these tables to this module:

* ``lead``: one LinkedIn lead-form response, linked to the campaign and (where LinkedIn
  reports it) the creative that produced it, with its form identity and the SHA-256 hash of
  its normalised email.
* ``lead_match``: the durable matching state of one lead against one CRM dataset, keyed on
  (``lead_id``, ``crm_dataset_id``).
* ``crm_contact``, ``crm_meeting`` and ``crm_deal``: the HubSpot objects the attribution
  chain reads (AAP 0.5.4.7).
* ``crm_association``: one association between two HubSpot objects.
* ``crm_association_fetch``: the outcome of reading one object's associations to one target
  type, together with the outcome of reading the associated targets.

**Personal data** (AAP 0.3.2, 0.3.3): lead and contact emails are persisted only as SHA-256
hex digests of the normalised (trimmed, lowercased) address. The schema has no plaintext
email column under any name; the HubSpot matching job re-fetches each email from LinkedIn
transiently and holds it in memory only for the call that needs it. Free-text form answers
are not stored either.

**Datasets and purge** (AAP 0.3.3): every table except ``lead_match`` mixes in
:class:`~ads_engine.db.base.DatasetScopedMixin`, and every foreign key between rows of the
same dataset is ``ON DELETE CASCADE``. ``lead``'s references to its campaign and creative
are dataset-qualified composite foreign keys, (``campaign_id``, ``dataset_id``) ->
``campaign`` (``id``, ``dataset_id``) and (``creative_id``, ``dataset_id``) -> ``creative``
(``id``, ``dataset_id``), each backed by the parent's unique (``dataset_id``, ``id``), so a
lead can only reference entities of its own dataset. ``lead_match`` is the one table that
links two datasets, a LinkedIn lead and a CRM contact, so it carries ``crm_dataset_id``
instead of the mixin, and all three of its foreign keys cascade. Its contact key is
(``contact_id``, ``crm_dataset_id``) -> ``crm_contact`` (``id``, ``dataset_id``), so a match
can only reference a contact of its own CRM dataset, and a purge of another CRM dataset can
never delete it. Purging a retired HubSpot dataset therefore deletes only that dataset's own
matching rows, and purging a retired LinkedIn dataset deletes its leads with every match
that references them. The first referencing column of every foreign key is indexed or leads
an index, because PostgreSQL does not index referencing columns and the cascade would
otherwise scan the child table once per deleted parent row.

**Unknown is never absent** (AAP 0.3.3, 0.5.4.7): association endpoints are HubSpot ids
stored as ``text`` with no foreign key, so an association whose meeting or deal could not be
read is still recorded. ``crm_association_fetch`` then says whether that evidence is known,
in exactly two cases: an ``ok_empty`` fetch is a known genuine zero, with no target reads
(its ``target_read_status`` is ``None``); an ``ok`` fetch is known only when its
``target_read_status`` is ``ok``. Anything else (``partial``, ``error``, or ``ok`` with
target reads ``partial``, ``error`` or ``None``) makes the contact's evidence unknown, never
zero.

HubSpot ids are ``text`` (they exceed 2**53 and arrive as strings). CRM property columns are
nullable, and ``None`` means unavailable or not returned, never a default (AAP 0.11.1).
Raw portal values (``crm_contact.lifecycle_stage``, ``crm_meeting.outcome`` and
``meeting_source``, ``crm_deal.stage`` and ``pipeline``) are stored exactly as HubSpot returns
them, with no ``CHECK``, because portals customise them. The product's own enumerations are
``text`` guarded by :func:`~ads_engine.db.base.check_in`.

Each dataset-scoped model declares ``__natural_key__``, the columns of its natural-key unique
constraint, which ``db/repositories.py`` uses as the ``ON CONFLICT`` target of its idempotent
upserts. ``backend/migrations/versions/0001_initial_schema.py`` mirrors every column, type,
nullability, server default, constraint and index name declared here; ``alembic check`` must
report no drift.
"""

from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Final

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ads_engine.db.base import Base, DatasetScopedMixin, IdPkMixin, check_in
from ads_engine.db.models.ops import SOURCES

__all__ = [
    "ASSOCIATION_FETCH_ERROR",
    "ASSOCIATION_FETCH_OK",
    "ASSOCIATION_FETCH_OK_EMPTY",
    "ASSOCIATION_FETCH_PARTIAL",
    "ASSOCIATION_FETCH_STATUSES",
    "CONTACT_READ_ERROR",
    "CONTACT_READ_OK",
    "CONTACT_READ_STATUSES",
    "CRM_OBJECT_CONTACT",
    "CRM_OBJECT_DEAL",
    "CRM_OBJECT_MEETING",
    "CRM_OBJECT_TYPES",
    "LEAD_MATCH_CLAIMABLE_SQL",
    "LEAD_MATCH_ERROR",
    "LEAD_MATCH_MATCHED",
    "LEAD_MATCH_NO_EMAIL",
    "LEAD_MATCH_NO_MATCH",
    "LEAD_MATCH_PENDING",
    "LEAD_MATCH_STATUSES",
    "TARGET_READ_ERROR",
    "TARGET_READ_OK",
    "TARGET_READ_PARTIAL",
    "TARGET_READ_STATUSES",
    "CrmAssociation",
    "CrmAssociationFetch",
    "CrmContact",
    "CrmDeal",
    "CrmMeeting",
    "Lead",
    "LeadMatch",
]

LEAD_MATCH_PENDING: Final = "pending"
"""A lead match not attempted yet."""
LEAD_MATCH_MATCHED: Final = "matched"
"""A lead match that found its contact."""
LEAD_MATCH_NO_MATCH: Final = "no_match"
"""A lead match that found no contact; retried on the :class:`LeadMatch` schedule."""
LEAD_MATCH_NO_EMAIL: Final = "no_email"
"""A lead with no decodable email; terminal."""
LEAD_MATCH_ERROR: Final = "error"
"""A lead match whose lookup failed; retried on the :class:`LeadMatch` schedule."""

LEAD_MATCH_STATUSES: Final[tuple[str, ...]] = (
    LEAD_MATCH_PENDING,
    LEAD_MATCH_MATCHED,
    LEAD_MATCH_NO_MATCH,
    LEAD_MATCH_NO_EMAIL,
    LEAD_MATCH_ERROR,
)
"""State of one lead's match against one CRM dataset (AAP 0.3.3).

``pending`` has not been attempted yet; ``matched`` found a contact; ``no_match`` and
``error`` are retried on the schedule in :class:`LeadMatch`; ``no_email`` is terminal. Every
status other than ``matched`` counts as unresolved in attribution shares (AAP 0.5.4.1).
"""

CONTACT_READ_OK: Final = "ok"
"""The matched contact's latest refresh by id read it."""
CONTACT_READ_ERROR: Final = "error"
"""The matched contact's latest refresh by id did not read it: its stage dates are unknown."""

CONTACT_READ_STATUSES: Final[tuple[str, ...]] = (CONTACT_READ_OK, CONTACT_READ_ERROR)
"""Outcome of the latest refresh of a matched contact by id: ``error`` makes its stage dates
unknown for the window rather than zero (AAP 0.3.3, 0.5.4.7)."""

ASSOCIATION_FETCH_OK: Final = "ok"
"""The association read returned associations."""
ASSOCIATION_FETCH_OK_EMPTY: Final = "ok_empty"
"""The association read succeeded with none: a genuine zero."""
ASSOCIATION_FETCH_PARTIAL: Final = "partial"
"""A continuation page of the association read failed."""
ASSOCIATION_FETCH_ERROR: Final = "error"
"""The association read of the input failed."""

ASSOCIATION_FETCH_STATUSES: Final[tuple[str, ...]] = (
    ASSOCIATION_FETCH_OK,
    ASSOCIATION_FETCH_OK_EMPTY,
    ASSOCIATION_FETCH_PARTIAL,
    ASSOCIATION_FETCH_ERROR,
)
"""Outcome of one association batch read for one (object, target type) pair (AAP 0.3.3).

``ok`` returned associations; ``ok_empty`` succeeded with none, a genuine zero (HubSpot
reports it as a 207 ``NO_ASSOCIATIONS_FOUND`` error, which the parser maps here);
``partial`` means a continuation page failed; ``error`` means the input failed.
"""

TARGET_READ_OK: Final = "ok"
"""Every associated id was read."""
TARGET_READ_PARTIAL: Final = "partial"
"""Some associated ids were not read."""
TARGET_READ_ERROR: Final = "error"
"""No associated id was read."""

TARGET_READ_STATUSES: Final[tuple[str, ...]] = (
    TARGET_READ_OK,
    TARGET_READ_PARTIAL,
    TARGET_READ_ERROR,
)
"""Outcome of batch-reading the associated meetings or deals by id (AAP 0.3.3): ``ok`` when
every associated id was returned, ``partial`` when some were missing or a call failed,
``error`` when none were read."""

CRM_OBJECT_CONTACT: Final = "contact"
"""A HubSpot contact."""
CRM_OBJECT_MEETING: Final = "meeting"
"""A HubSpot meeting."""
CRM_OBJECT_DEAL: Final = "deal"
"""A HubSpot deal."""

CRM_OBJECT_TYPES: Final[tuple[str, ...]] = (
    CRM_OBJECT_CONTACT,
    CRM_OBJECT_MEETING,
    CRM_OBJECT_DEAL,
)
"""HubSpot object types, singular; the parsers map HubSpot's plural path names to these."""

# The CHECK text the migration repeats verbatim: a stored hash is exactly 64 lower-case hex
# characters, the SHA-256 digest of the normalised email. NULL (no email) passes.
_EMAIL_SHA256_HEX_SQL: Final[str] = "email_sha256 ~ '^[0-9a-f]{64}$'"

# The completed-state CHECK texts the migration repeats verbatim. A ``matched`` lead_match row
# names its contact and the outcome of reading it; every other status is left unconstrained.
_MATCHED_HAS_CONTACT_SQL: Final[str] = (
    "status <> 'matched' OR (contact_id IS NOT NULL AND contact_read_status IS NOT NULL)"
)
# An ``ok_empty`` fetch (a genuine zero) has no targets to read and none missing.
_OK_EMPTY_SHAPE_SQL: Final[str] = (
    "status <> 'ok_empty' OR (target_read_status IS NULL AND cardinality(missing_target_ids) = 0)"
)
# Target reads are ``ok`` only when every associated id was returned, whatever the status.
_TARGET_READ_OK_COMPLETE_SQL: Final[str] = (
    "target_read_status <> 'ok' OR cardinality(missing_target_ids) = 0"
)

LEAD_MATCH_CLAIMABLE_SQL: Final[str] = (
    "(lead_match.status = 'pending' OR (lead_match.status IN ('no_match', 'error')"
    " AND lead_match.next_attempt_at IS NOT NULL))"
)
"""The ``lead_match`` rows matching can still claim: the predicate of ``ix_lead_match_claim``.

``pending`` rows, and ``no_match`` or ``error`` rows with a retry scheduled. ``matched``,
``no_email`` and rows whose retries stopped (``next_attempt_at IS NULL``) are terminal and
stay out of the index, so they never sort ahead of due rows in its ``NULLS FIRST`` order.
The migration repeats this text verbatim, and the claim in ``sync/lead_match.py`` uses it
as-is next to its due-time test, so PostgreSQL proves the partial index applies to the
claim under custom and generic plans alike; status tests sent as bind parameters could not
prove it under a generic plan. Columns are table-qualified because the claim joins tables
that also have a ``status``, and the outer parentheses keep the text one term wherever it
is combined with other criteria.
"""


class Lead(IdPkMixin, DatasetScopedMixin, Base):
    """One LinkedIn lead-form response, traced to the campaign and creative that produced it.

    AAP 0.5.2 (``lead``), 0.3.3 (Lead Sync, form identity, plaintext handling) and 0.5.4.7
    (attribution chain). Rows are written by the LinkedIn lead job, de-duplicated on
    :attr:`__natural_key__`. Test leads (``testLead = true``) are dropped at ingest and never
    stored, so every row is a real sponsored lead.

    ``lead_form_urn`` is the canonical form URN, so every version of a form resolves to the
    same content asset; ``lead_form_version`` is parsed only from the response's
    ``versionedLeadGenFormUrn`` and selects the email question map in ``lead_form_version``.

    The email is stored only as ``email_sha256``, guarded by ``ck_lead_email_sha256_hex``.
    It is ``None`` when the response carried no decodable email; the lead's ``lead_match``
    row then records ``no_email`` with the reason ("form map mismatch" or "unknown form
    version"). Each new lead gets a ``pending`` :class:`LeadMatch` row.

    Both entity references are dataset-qualified: the composite foreign keys
    (``campaign_id``, ``dataset_id``) and (``creative_id``, ``dataset_id``) reference each
    entity's unique (``dataset_id``, ``id``), so a lead can reference only a campaign and a
    creative of its own dataset. Only same-dataset lineage therefore enters attribution, and
    purging another dataset can never delete the lead. A ``NULL`` ``creative_id`` is not
    checked (``MATCH SIMPLE``). ``ix_lead_campaign_id`` and ``ix_lead_creative_id`` serve the
    cascades from ``campaign`` and ``creative``.

    Attributes:
        external_id: The LinkedIn response id, a plain id rather than a URN.
        submitted_at: The response's ``submittedAt`` instant: the touch that anchors the
            causal check and lead maturity (AAP 0.5.4.1, 0.5.4.7).
        campaign_id: The campaign from the response's sponsored lead metadata, of the lead's
            own dataset.
        creative_id: The creative from ``associatedEntity``, of the lead's own dataset;
            ``None`` when LinkedIn reports none, which counts toward the creative share
            (``creative_floor``).
        lead_form_urn: The canonical ``urn:li:leadGenForm:{id}``.
        lead_form_version: The integer form version parsed from ``versionedLeadGenFormUrn``.
        email_sha256: SHA-256 hex digest of the normalised email; ``None`` without one.
    """

    __tablename__ = "lead"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "external_id")

    external_id: Mapped[str]
    submitted_at: Mapped[datetime]
    campaign_id: Mapped[int] = mapped_column(index=True)
    creative_id: Mapped[int | None] = mapped_column(index=True)
    lead_form_urn: Mapped[str]
    lead_form_version: Mapped[int]
    email_sha256: Mapped[str | None] = mapped_column(sa.CHAR(64))

    __table_args__ = (
        check_in("source", SOURCES),
        sa.CheckConstraint(_EMAIL_SHA256_HEX_SQL, name="email_sha256_hex"),
        # Dataset-qualified: the campaign and the creative must belong to this lead's dataset.
        sa.ForeignKeyConstraint(
            ["campaign_id", "dataset_id"],
            ["campaign.id", "campaign.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["creative_id", "dataset_id"],
            ["creative.id", "creative.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dataset_id", "external_id"),
        # Window cohorts select a dataset's leads by submission instant.
        sa.Index("ix_lead_dataset_submitted", "dataset_id", "submitted_at"),
    )


class LeadMatch(Base):
    """Durable matching state of one lead against one CRM dataset.

    AAP 0.5.2 (``lead_match``) and 0.3.3 (lead-to-contact matching). The primary key is
    (``lead_id``, ``crm_dataset_id``), so a ``building`` CRM dataset stages its own matches
    while the active dataset's rows stay in use, and one transaction later activates the
    building dataset with its matching state. The table has no surrogate ``id`` and no
    ``DatasetScopedMixin``: it links a LinkedIn lead to a contact in ``crm_dataset_id``.

    ``sync/lead_match.py`` writes these rows directly rather than through a natural-key
    upsert, claiming due rows with ``FOR UPDATE SKIP LOCKED`` and committing per batch, so a
    crash mid-job loses no state. The partial index ``ix_lead_match_claim`` on
    (``crm_dataset_id``, ``next_attempt_at NULLS FIRST``, ``lead_id``) serves that claim: it
    holds only the rows :data:`LEAD_MATCH_CLAIMABLE_SQL` admits, in the claim's own order, so
    each batch reads its rows in order and stops at its limit rather than sorting every due
    row. ``ix_lead_match_due`` serves the per-status counts of the readiness test and the
    cascade from ``source_dataset``; the primary key serves the cascade from ``lead``, and
    ``ix_lead_match_contact_id`` the cascade from ``crm_contact``.

    Retry schedule: ``no_match`` and ``error`` rows are retried after 1 h, 6 h and 24 h, then
    daily until 30 days after submission, so contacts created late are still found.
    ``no_email`` is terminal. An email LinkedIn no longer returns ends as ``error`` with the
    reason "email no longer retrievable".

    Completed state: a row becomes ``matched`` together with its ``contact_id`` and
    ``contact_read_status``, enforced by ``ck_lead_match_matched_has_contact``. The email
    batch read that finds the contact is itself a successful read of it, so the matching
    writer sets ``ok``; a later failed refresh by id sets ``error``. ``pending``,
    ``no_match``, ``no_email`` and ``error`` rows are not constrained further.

    Contact and purge: the contact key is the composite (``contact_id``,
    ``crm_dataset_id``) -> ``crm_contact`` (``id``, ``dataset_id``), so ``contact_id`` can
    only name a contact of the match's own CRM dataset; the database rejects a contact of
    any other dataset, and a ``NULL`` ``contact_id`` is allowed. All three foreign keys
    (``lead_id``, ``crm_dataset_id`` and the contact key) are ``ON DELETE CASCADE``.
    Purging a retired HubSpot dataset therefore deletes only its own rows, through
    ``crm_dataset_id`` and its own contacts, and never a row staged for another CRM
    dataset; purging a retired LinkedIn dataset deletes every row of its leads.

    Attributes:
        lead_id: The :class:`Lead` being matched.
        crm_dataset_id: The CRM ``source_dataset`` the match belongs to: HubSpot's primary
            dataset, or the companion of a simulated LinkedIn dataset.
        status: ``pending``, ``matched``, ``no_match``, ``no_email`` or ``error``
            (:data:`LEAD_MATCH_STATUSES`).
        attempts: Match attempts made so far (server default ``0``).
        next_attempt_at: When the row is next due; ``None`` when no retry is scheduled.
        last_checked_at: When a match was last attempted; ``None`` while never attempted.
        contact_id: The matched :class:`CrmContact`, a contact in ``crm_dataset_id``
            (enforced by ``fk_lead_match_contact_id_crm_contact``); ``None`` until matched.
        contact_read_status: Outcome of the latest refresh of the matched contact
            (:data:`CONTACT_READ_STATUSES`); ``None`` until matched.
        reason: Why the row is in its status, for example "form map mismatch".
    """

    __tablename__ = "lead_match"

    lead_id: Mapped[int] = mapped_column(
        sa.ForeignKey("lead.id", ondelete="CASCADE"),
        primary_key=True,
    )
    crm_dataset_id: Mapped[int] = mapped_column(
        sa.ForeignKey("source_dataset.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str]
    attempts: Mapped[int] = mapped_column(server_default=sa.text("0"))
    # Bound to a name (not annotation-only) so __table_args__ can index it NULLS FIRST.
    next_attempt_at: Mapped[datetime | None] = mapped_column()
    last_checked_at: Mapped[datetime | None]
    contact_id: Mapped[int | None] = mapped_column(index=True)
    contact_read_status: Mapped[str | None]
    reason: Mapped[str | None]

    __table_args__ = (
        check_in("status", LEAD_MATCH_STATUSES),
        check_in("contact_read_status", CONTACT_READ_STATUSES),
        # A matched row always carries its contact and that contact's read status.
        sa.CheckConstraint(_MATCHED_HAS_CONTACT_SQL, name="matched_has_contact"),
        # The contact must belong to this match's CRM dataset. contact_id is listed first so
        # the convention names the key fk_lead_match_contact_id_crm_contact; a NULL
        # contact_id (no match yet) is not checked.
        sa.ForeignKeyConstraint(
            ["contact_id", "crm_dataset_id"],
            ["crm_contact.id", "crm_contact.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.Index("ix_lead_match_due", "crm_dataset_id", "status", "next_attempt_at"),
        # The claim's due rows in its exact order (next_attempt_at NULLS FIRST, lead_id),
        # holding claimable rows only, so no terminal row is ever read ahead of a due one.
        sa.Index(
            "ix_lead_match_claim",
            "crm_dataset_id",
            next_attempt_at.asc().nulls_first(),
            "lead_id",
            postgresql_where=sa.text(LEAD_MATCH_CLAIMABLE_SQL),
        ),
    )


class CrmContact(IdPkMixin, DatasetScopedMixin, Base):
    """A HubSpot contact matched to a lead or discovered through its Google touch.

    AAP 0.5.2 (``crm_contact``), 0.3.3 (contact refresh, Google-to-CRM link) and 0.5.4.7
    (contact to MQL and QL). Contacts are read by email batch read during matching, then
    refreshed by id. Simulated contacts carry ids from a reserved range, so they never
    collide with live HubSpot ids, and leave every computation when their dataset retires.

    ``mql_at`` and ``ql_at`` hold the dates of the stage-entry properties bound in
    ``config/hubspot_mapping.yaml`` and resolved against the portal at startup. An empty
    date means the contact skipped that stage: it is not counted, even if its current stage
    is later, and no other date stands in. QL is observed from its own property and never
    derived from MQL.

    ``uq_crm_contact_dataset_id_id`` makes (``dataset_id``, ``id``) a key that
    :class:`LeadMatch` references, so a match can only point at a contact of its own CRM
    dataset. ``ix_crm_contact_dataset_google_touch``, partial on ``google_campaign_ref IS NOT
    NULL``, serves Google-touch discovery: a dataset's Google-linked contacts within a window,
    read as a ``google_touch_at`` range without visiting the contacts that carry no ref.

    Attributes:
        hs_id: The HubSpot contact id.
        email_sha256: SHA-256 hex digest of the contact's normalised email, the join key of
            lead matching; ``None`` when the contact has no email.
        lifecycle_stage: The raw ``lifecyclestage`` value (a stage id; custom stages are
            numeric), stored as returned.
        mql_at: When the contact entered the bound MQL stage; ``None`` if it never did.
        ql_at: When the contact entered the bound Qualified Lead stage; ``None`` if it never
            did.
        google_campaign_ref: The Google campaign id held in
            ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY``; ``None`` when not configured or empty.
        google_touch_at: The instant held in ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY``, which
            anchors the causal check for Google; ``None`` when not configured or empty.
        updated_at: The record's ``updatedAt`` as HubSpot reports it; ``None`` if absent.
    """

    __tablename__ = "crm_contact"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "hs_id")

    hs_id: Mapped[str]
    email_sha256: Mapped[str | None] = mapped_column(sa.CHAR(64))
    lifecycle_stage: Mapped[str | None]
    mql_at: Mapped[datetime | None]
    ql_at: Mapped[datetime | None]
    google_campaign_ref: Mapped[str | None]
    google_touch_at: Mapped[datetime | None]
    updated_at: Mapped[datetime | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "hs_id"),
        # Anchors lead_match's dataset-qualified contact key, (contact_id, crm_dataset_id).
        sa.UniqueConstraint("dataset_id", "id"),
        # Lead matching and simulated contact lookups find a dataset's contact by email hash.
        sa.Index("ix_crm_contact_dataset_email", "dataset_id", "email_sha256"),
        # Google-touch discovery (analysis.metrics._google_touch_contacts): one dataset's
        # contacts with a campaign ref, by touch-instant range, in touch order.
        sa.Index(
            "ix_crm_contact_dataset_google_touch",
            "dataset_id",
            "google_touch_at",
            postgresql_where=sa.text("google_campaign_ref IS NOT NULL"),
        ),
    )


class CrmMeeting(IdPkMixin, DatasetScopedMixin, Base):
    """A HubSpot meeting associated with a matched contact.

    AAP 0.5.2 (``crm_meeting``) and 0.5.4.7 (causal ordering check). HubSpot records no
    booking instant, so ``created_at`` (``hs_createdate``, the record's creation time) is
    the labelled proxy "booked (record created)". It is HubSpot's own timestamp, never a
    database default. A meeting counts as booked after a touch only when ``created_at`` and
    ``start_at`` are both at or after the anchor, its ``outcome`` is not excluded
    (``crm.meeting_outcome_exclusions``) and its ``meeting_source`` is a booking source
    (``crm.booking_sources``); meetings from other sources are reported as "ordering
    unverified".

    Attributes:
        hs_id: The HubSpot meeting id.
        created_at: ``hs_createdate``, the booking proxy; ``None`` if not returned.
        start_at: ``hs_meeting_start_time``; ``None`` if not returned.
        meeting_timestamp: ``hs_timestamp``; ``None`` if not returned.
        outcome: The raw ``hs_meeting_outcome``, for example ``CANCELED``.
        meeting_source: The raw meeting source, for example ``MEETINGS_PUBLIC``.
    """

    __tablename__ = "crm_meeting"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "hs_id")

    hs_id: Mapped[str]
    created_at: Mapped[datetime | None]
    start_at: Mapped[datetime | None]
    meeting_timestamp: Mapped[datetime | None]
    outcome: Mapped[str | None]
    meeting_source: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "hs_id"),
    )


class CrmDeal(IdPkMixin, DatasetScopedMixin, Base):
    """A HubSpot deal, read through a contact association or the window deal search.

    AAP 0.5.2 (``crm_deal``), 0.3.3 (deal search) and 0.5.4.7 (causal check, orphan deals).
    ``created_at`` is HubSpot's ``createdate``, the event the causal check names: a deal
    counts only when it was created at or after the contact's touch. It is never a database
    default. Deals created from window start to run time are found by partitioned CRM Search
    so the orphan-deal share can be measured; ``ix_crm_deal_dataset_created`` serves that
    range scan.

    Attributes:
        hs_id: The HubSpot deal id.
        created_at: ``createdate``; ``None`` if not returned.
        stage: The raw ``dealstage``.
        pipeline: The raw ``pipeline``.
        amount: The deal ``amount``; ``None`` when not set.
    """

    __tablename__ = "crm_deal"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "hs_id")

    hs_id: Mapped[str]
    created_at: Mapped[datetime | None]
    stage: Mapped[str | None]
    pipeline: Mapped[str | None]
    amount: Mapped[Decimal | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.UniqueConstraint("dataset_id", "hs_id"),
        sa.Index("ix_crm_deal_dataset_created", "dataset_id", "created_at"),
    )


class CrmAssociation(IdPkMixin, DatasetScopedMixin, Base):
    """One association between two HubSpot objects, as read by an association batch read.

    AAP 0.5.2 (``crm_association``) and 0.3.3 (associations). Meetings and deals relate to
    contacts many-to-many in both directions, through rows of this table. ``from_id`` and
    ``to_id`` are HubSpot ids with no foreign key: an association whose target meeting or
    deal could not be read must still be stored, so that the target is treated as unknown,
    never as absent. Whether an association is known evidence is recorded on the matching
    :class:`CrmAssociationFetch` row.

    Attributes:
        from_type: Type of the object the association was read from
            (:data:`CRM_OBJECT_TYPES`).
        from_id: HubSpot id of that object.
        to_type: Type of the associated object (:data:`CRM_OBJECT_TYPES`).
        to_id: HubSpot id of the associated object.
    """

    __tablename__ = "crm_association"
    __natural_key__: ClassVar[tuple[str, ...]] = (
        "dataset_id",
        "from_type",
        "from_id",
        "to_type",
        "to_id",
    )

    from_type: Mapped[str]
    from_id: Mapped[str]
    to_type: Mapped[str]
    to_id: Mapped[str]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("from_type", CRM_OBJECT_TYPES),
        check_in("to_type", CRM_OBJECT_TYPES),
        sa.UniqueConstraint(
            "dataset_id",
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            name="uq_crm_association_tuple",
        ),
        # Reverse lookups (the contacts of a meeting or deal) go by target.
        sa.Index("ix_crm_association_target", "dataset_id", "to_type", "to_id"),
    )


class CrmAssociationFetch(IdPkMixin, DatasetScopedMixin, Base):
    """Outcome of reading one object's associations to one target type, and of its targets.

    AAP 0.5.2 (``crm_association_fetch``), 0.3.3 (fetch status, target reads) and 0.5.4.7
    (unknown CRM evidence, orphan-deal share). One row per (object, target type) fetch, for
    example a contact's meetings or a deal's contacts.

    A fetch is known evidence in exactly two cases. ``status`` ``ok_empty`` is a known
    genuine zero: there is nothing to read, so ``target_read_status`` is ``None``. ``status``
    ``ok`` is known only when ``target_read_status`` is ``ok``. Anything else (``partial``,
    ``error``, or ``ok`` with target reads ``partial``, ``error`` or ``None``) is unknown,
    never absent or zero, and makes the contact's evidence unknown. A deal's orphan status
    comes from its deal-to-contact fetch on the same rule: ``ok_empty`` means orphan, ``ok``
    with target reads ``ok`` means linked, and anything else is unknown.

    Two checks keep each stored outcome consistent with its meaning:
    ``ck_crm_association_fetch_ok_empty_shape`` gives an ``ok_empty`` fetch a ``None``
    ``target_read_status`` and an empty ``missing_target_ids``, and
    ``ck_crm_association_fetch_target_read_ok_complete`` allows ``target_read_status`` ``ok``
    only with an empty ``missing_target_ids``, whatever the ``status``. ``partial`` and
    ``error`` fetches are not constrained further.

    Attributes:
        object_type: Type of the object whose associations were read
            (:data:`CRM_OBJECT_TYPES`).
        object_id: HubSpot id of that object, with no foreign key.
        to_type: The target type read (:data:`CRM_OBJECT_TYPES`).
        status: ``ok``, ``ok_empty``, ``partial`` or ``error``
            (:data:`ASSOCIATION_FETCH_STATUSES`).
        target_read_status: ``ok``, ``partial`` or ``error`` (:data:`TARGET_READ_STATUSES`);
            ``None`` when ``status`` is ``ok_empty`` or the targets were not read.
        missing_target_ids: Associated ids the target batch read did not return (server
            default ``'{}'::text[]``).
        fetched_at: When the fetch ran.
        error: The failure detail of a failed or partial fetch, with secrets redacted.
    """

    __tablename__ = "crm_association_fetch"
    __natural_key__: ClassVar[tuple[str, ...]] = (
        "dataset_id",
        "object_type",
        "object_id",
        "to_type",
    )

    object_type: Mapped[str]
    object_id: Mapped[str]
    to_type: Mapped[str]
    status: Mapped[str]
    target_read_status: Mapped[str | None]
    missing_target_ids: Mapped[list[str]] = mapped_column(server_default=sa.text("'{}'::text[]"))
    fetched_at: Mapped[datetime]
    error: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("object_type", CRM_OBJECT_TYPES),
        check_in("to_type", CRM_OBJECT_TYPES),
        check_in("status", ASSOCIATION_FETCH_STATUSES),
        check_in("target_read_status", TARGET_READ_STATUSES),
        # ok_empty is a genuine zero: no target-read status and no missing target.
        sa.CheckConstraint(_OK_EMPTY_SHAPE_SQL, name="ok_empty_shape"),
        # Target reads ok means every associated id was returned, so none is missing.
        sa.CheckConstraint(_TARGET_READ_OK_COMPLETE_SQL, name="target_read_ok_complete"),
        sa.UniqueConstraint(
            "dataset_id",
            "object_type",
            "object_id",
            "to_type",
            name="uq_crm_association_fetch_object",
        ),
    )
