"""Analysis tables: runs, run requests, frozen run outputs and recommendation feedback.

AAP 0.5.2 assigns these tables to this module:

* ``analysis_run``: one computation of the five sections over one window, ``default``
  (the worker's run of the configured default window) or ``custom`` (``POST /runs`` or
  ``ads-engine analyze``). At most one run is ``running`` at a time.
* ``analysis_request``: the queue the worker coalesces (within 60 s) into default-window
  runs after every sync, dataset activation, decision, discrepancy or feedback write.
* ``recommendation``: the ranked action items of a run, each with its frozen evidence.
* ``run_finding``: non-action observations a rule emits, such as ``k1_no_baseline``.
* ``run_withheld``: patterns withheld for sample size, with observed and required sizes.
* ``run_suppression``: per-section counts of items hidden by feedback, blocked by the G1
  guard or suppressed by the P2 tracking finding.
* ``recommendation_feedback``: dismiss, snooze and done, which suppress the same rule and
  entity in later runs until ``until``.

Run outputs are written once by the runner, in one transaction, and never mutated
(AAP 0.5.3 "Where outputs are frozen"); only the read-time overlay is computed live. Their
evidence is denormalised in ``jsonb``, together with the provenance dataset ids, so a run
still renders after the datasets it read are purged, as historical-only (AAP 0.3.2).

These tables are not source-derived: they carry no ``source`` or ``dataset_id`` and are not
reached by the dataset purge's cascade. Their foreign-key actions (AAP 0.5.2) are:

* every run-output table references ``analysis_run`` with ``ON DELETE CASCADE``;
* their entity keys (``campaign_id``, ``budget_id``, ``ad_group_id``, ``keyword_id`` and
  ``creative_id``) are nullable and ``ON DELETE SET NULL``, so purging a retired dataset
  nulls the keys while the evidence keeps names and numbers;
* ``analysis_request.run_id`` is ``ON DELETE SET NULL``;
* ``recommendation_feedback`` references ``recommendation`` only, with ``ON DELETE CASCADE``;
* ``analysis_run.dataset_ids`` is ``jsonb`` with no foreign key.

Enumerations are ``text`` guarded by :func:`~ads_engine.db.base.check_in` over the tuples
exported here. ``backend/migrations/versions/0001_initial_schema.py`` mirrors every column,
type, nullability, server default, constraint and index name declared here; ``alembic check``
must report no drift.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

# Imported as a module and always written ``sa.<name>``, as in the sibling model modules.
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ads_engine.db.base import Base, IdPkMixin, check_in

__all__ = [
    "DATA_STATUSES",
    "FEEDBACK_KINDS",
    "RECOMMENDATION_SECTIONS",
    "RUN_KINDS",
    "RUN_STATUSES",
    "RUN_TRIGGERS",
    "AnalysisRequest",
    "AnalysisRun",
    "Recommendation",
    "RecommendationFeedback",
    "RunFinding",
    "RunSuppression",
    "RunWithheld",
]

RUN_KINDS: Final[tuple[str, ...]] = ("default", "custom")
"""``default`` is the worker's run of the configured default window; ``custom`` is any run
with an explicit window, even one equal to the default (AAP 0.5.3 run kinds).

``/runs/latest`` and ``GET /recommendations`` without ``run_id`` return only the latest
succeeded ``default`` run, so a manual run never stands in for the default.
"""

RUN_TRIGGERS: Final[tuple[str, ...]] = ("scheduled", "event", "manual")
"""What started a run: the schedule, a coalesced ``analysis_request`` event, or a user
(``POST /runs`` or ``ads-engine analyze``)."""

RUN_STATUSES: Final[tuple[str, ...]] = ("running", "succeeded", "failed")
"""Run lifecycle. A ``running`` row left by a crashed process is marked ``failed`` by the
next process that takes the ``analysis`` lock (AAP 0.5.3 crash recovery)."""

FEEDBACK_KINDS: Final[tuple[str, ...]] = ("dismiss", "snooze", "done")
"""Feedback on an item: dismiss (30 days), snooze (7 days) or mark done (30 days)."""

DATA_STATUSES: Final[tuple[str, ...]] = ("live", "simulated", "stale", "mixed")
"""Data status stored on every recommendation (AAP 0.3.3 labelling). ``mixed`` marks an item
built on a join between a live and a simulated dataset."""

RECOMMENDATION_SECTIONS: Final[tuple[str, ...]] = (
    "keywords",
    "campaigns",
    "budget",
    "creative",
    "white_papers",
)
"""The five sections, in the fixed order the API returns and the UI renders them
(AAP 0.5.3 ``RecommendationsPage``). ``analysis/context.py`` must use equal values."""


class _RunEntityKeysMixin:
    """Typed, nullable entity keys of a run-output row, each ``ON DELETE SET NULL``.

    AAP 0.5.2: "Keys from run tables (``recommendation``, ``run_finding``,
    ``run_withheld``) to entities are nullable and ``ON DELETE SET NULL``." One typed column
    per referenced entity table implements that contract, which a single polymorphic id could
    not: a foreign key needs one target table. A row fills the keys of the entities it is
    about (a keyword item fills ``keyword_id`` and may fill its ``ad_group_id`` and
    ``campaign_id``) and leaves the others ``NULL``.

    Purging a retired dataset deletes these entities through their ``dataset_id`` cascade,
    and PostgreSQL then sets the matching keys here to ``NULL`` row by row. PostgreSQL does
    not index referencing columns and the run tables grow without bound, so every key is
    indexed (``ix_<table>_<column>``) to keep that update from scanning the whole table.

    The columns are copied into each model that mixes this in. SQLAlchemy places mixin
    columns after the model's own columns, so they are the last five columns of each table.

    Attributes:
        campaign_id: The :class:`~ads_engine.db.models.accounts.Campaign` the row is about.
        budget_id: The Google :class:`~ads_engine.db.models.accounts.Budget` the row is
            about, such as a shared budget subject.
        ad_group_id: The Google :class:`~ads_engine.db.models.accounts.AdGroup` the row is
            about.
        keyword_id: The Google :class:`~ads_engine.db.models.accounts.Keyword` the row is
            about.
        creative_id: The LinkedIn :class:`~ads_engine.db.models.accounts.Creative` the row is
            about.
    """

    campaign_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("campaign.id", ondelete="SET NULL"),
        index=True,
    )
    budget_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("budget.id", ondelete="SET NULL"),
        index=True,
    )
    ad_group_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("ad_group.id", ondelete="SET NULL"),
        index=True,
    )
    keyword_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("keyword.id", ondelete="SET NULL"),
        index=True,
    )
    creative_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("creative.id", ondelete="SET NULL"),
        index=True,
    )


class AnalysisRun(IdPkMixin, Base):
    """One analysis run: the five sections computed over one window from stored data.

    AAP 0.5.2 (``analysis_run``) and 0.5.3 (run selection and the analysis lock). The API,
    the worker and the CLI each take the session-level ``analysis`` advisory lock before
    inserting a ``running`` row; the partial unique index ``uq_analysis_run_single_running``
    is the second, database-enforced guard that allows only one ``running`` row at a time.
    ``ix_analysis_run_kind_status_finished`` serves ``/runs/latest`` (the latest succeeded
    ``default`` run). ``ix_analysis_run_kind_id`` on (``kind``, ``id DESC``) serves
    ``GET /runs?kind=custom``: the newest runs of one kind are one bounded index range read
    in order, however many runs of the other kind lie between them.

    ``data_modes`` and ``dataset_ids`` record the mode and the dataset id of each source as
    the run read them. They are ``jsonb`` with no foreign key, so run history survives the
    purge of those datasets; the overlay compares them with the current active datasets to
    mark items historical-only (AAP 0.3.2).

    Attributes:
        kind: ``default`` or ``custom`` (:data:`RUN_KINDS`).
        window_start: First calendar date of the window, applied to each source's native
            days (AAP 0.5.4.1).
        window_end: Last calendar date of the window, inclusive; never before
            ``window_start`` (``ck_analysis_run_window_order``).
        source_day_boundaries: Each source's range and day boundary, as
            ``[{source, start, end, boundary}]``; returned as ``Run.source_days``.
        trigger: ``scheduled``, ``event`` or ``manual`` (:data:`RUN_TRIGGERS`).
        data_modes: Mode per source, as ``{google, linkedin, hubspot}``.
        dataset_ids: Dataset ids the run read, per source and role; no foreign key.
        config_hash: Hash of the threshold, mapping and registry configuration used.
        status: ``running``, ``succeeded`` or ``failed`` (:data:`RUN_STATUSES`).
        started_at: When the run started.
        finished_at: When the run finished; ``None`` while ``running``.
    """

    __tablename__ = "analysis_run"

    kind: Mapped[str]
    window_start: Mapped[date]
    window_end: Mapped[date]
    # A list of objects, which the base type map (dict[str, Any] -> jsonb) does not cover.
    source_day_boundaries: Mapped[list[dict[str, Any]]] = mapped_column(JSONB())
    trigger: Mapped[str]
    data_modes: Mapped[dict[str, Any]]
    dataset_ids: Mapped[dict[str, Any]]
    config_hash: Mapped[str]
    status: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        check_in("kind", RUN_KINDS),
        check_in("trigger", RUN_TRIGGERS),
        check_in("status", RUN_STATUSES),
        sa.CheckConstraint("window_end >= window_start", name="window_order"),
        sa.Index(
            "uq_analysis_run_single_running",
            "status",
            unique=True,
            postgresql_where=sa.text("status = 'running'"),
        ),
        sa.Index("ix_analysis_run_kind_status_finished", "kind", "status", "finished_at"),
        # The newest runs of one kind, newest first (GET /runs, runner.list_runs).
        # ``id`` comes from IdPkMixin, so the class body names it rather than binding it.
        sa.Index("ix_analysis_run_kind_id", "kind", sa.desc("id")),
    )


class AnalysisRequest(IdPkMixin, Base):
    """A request for a default-window analysis run, queued after a change.

    AAP 0.3.2 (refresh after change) and 0.5.2 (``analysis_request``). A completed sync, a
    dataset activation or source-mode change, a decision write, a discrepancy being created
    or resolved, a feedback write, or a changed threshold file each enqueues a row. The worker
    coalesces requests that arrive within 60 s into one run and stamps ``picked_at`` and
    ``run_id`` on every request the run covers. Several pending requests are expected, so the
    partial index ``ix_analysis_request_pending`` over pending rows is not unique.

    When the ``analysis`` lock is held elsewhere, the worker leaves the request pending
    (AAP 0.5.3).

    Attributes:
        requested_at: When the request was made (server default ``now()``).
        reason: The event that caused it, for example ``decision`` or a sync job name.
        picked_at: When the worker picked the request up; ``None`` while pending.
        run_id: The :class:`AnalysisRun` that served the request; ``None`` while pending.
    """

    __tablename__ = "analysis_request"

    requested_at: Mapped[datetime] = mapped_column(server_default=sa.text("now()"))
    reason: Mapped[str]
    picked_at: Mapped[datetime | None]
    # Runs are not deleted in normal operation, so the SET NULL action needs no index here.
    run_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("analysis_run.id", ondelete="SET NULL"),
    )

    __table_args__ = (
        sa.Index(
            "ix_analysis_request_pending",
            "requested_at",
            postgresql_where=sa.text("picked_at IS NULL"),
        ),
    )


class Recommendation(IdPkMixin, _RunEntityKeysMixin, Base):
    """One ranked action item of a run, with the evidence that justifies it, frozen.

    AAP 0.5.2 (``recommendation``), 0.5.3 (``Item``) and 0.5.4.8 (evidence). The runner
    writes every item of a run once and never updates it; holds, flags and hidden state that
    arise later come from the read-time overlay (AAP 0.3.2), never from rewriting this row.
    ``ix_recommendation_run_section_rank`` serves the page read: one run's items by section
    in rank order. It leads with ``run_id``, so it also serves the cascade from
    :class:`AnalysisRun`.

    ``entity_type`` and ``entity_id`` name the item's entity as a stable, informational key
    with no foreign key, written from the rule's
    :class:`~ads_engine.analysis.context.EntityRef`. ``entity_type`` is its ``type``, for
    example ``campaign``, ``keyword`` or ``search_term``. ``entity_id`` is ``text``, its
    :attr:`~ads_engine.analysis.context.EntityRef.key` ``{source}:{type}:{external_id}``,
    for example ``google:campaign:1234567890``. ``external_id`` is the platform id of an
    entity with a table row (the account, campaign, ``campaign_budget`` or ad group id, a
    creative URN). A keyword has a row but takes ``{ad_group_external_id}|{criterion_id}``
    (:func:`~ads_engine.analysis.context.keyword_external_id`), because Google's criterion
    id is unique only within its ad group, giving keys such as
    ``google:keyword:123456789|987654``. An entity without a table row takes a composite: a
    search term ``{ad_group_external_id}|{matched_criterion_id}|{term}``, a demographic
    segment ``{campaign_external_id}|{facet}|{value}``, a white paper within one currency
    ``{slug}|{CURRENCY}``, a CR1 creative pair as its two creative URNs sorted and
    ``|``-joined, and a search-term cluster as its sorted head tokens joined by spaces. The
    key is built from platform ids, not internal row ids, so it survives a re-sync and a
    dataset transition of the same platform entity; only a C0 subject whose row the run's
    context lacks falls back to ``internal-{id}`` as its ``external_id``, and C0 takes no
    feedback. Feedback copies ``rule_id`` and the pair, so a dismissal, snooze or done
    matches the same rule and entity in later runs (AAP 0.5.2 ``recommendation_feedback``).

    Internal row ids live only in the nullable typed keys from :class:`_RunEntityKeysMixin`
    (``campaign_id``, ``budget_id``, ``ad_group_id``, ``keyword_id``, ``creative_id``). They
    carry the referential links within the run's datasets and become ``NULL`` when a purge
    removes the entity; ``entity_type``, ``entity_id`` and the evidence keep the item
    readable. A keyword item's ``keyword_id`` is its internal ``keyword`` row id and never
    part of its ``entity_id``, which is built from the platform ids above.

    Attributes:
        run_id: The :class:`AnalysisRun` that produced the item.
        section: The section the item belongs to (:data:`RECOMMENDATION_SECTIONS`).
        group: The group within the section, for example ``block``, ``pause`` or
            ``discrepancies``. A reserved word in SQL, so the column is quoted as
            ``"group"``.
        rule_id: The rule that produced the item, for example ``K1`` or ``C0``.
        entity_type: The kind of entity the item is about, for example ``campaign`` or
            ``search_term``; ``None`` for an item without an entity.
        entity_id: The entity's stable external key ``{source}:{type}:{external_id}``,
            built from platform ids rather than internal row ids, for example
            ``google:keyword:123456789|987654``; ``None`` for an item without an entity.
        rank: The item's rank within its group, from 1.
        action: The recommended action as a headline.
        reason: The reason sentence, rendered from the evidence by the rule's template.
        evidence: The frozen evidence block of AAP 0.5.3 ``Evidence``: window and source day
            boundaries, metrics, baselines, sample, test, thresholds, datasets and provenance.
        flags: Flag codes recorded at compute time (server default ``'{}'::text[]``).
        data_status: ``live``, ``simulated``, ``stale`` or ``mixed``
            (:data:`DATA_STATUSES`).
        held: Whether the item was held at compute time, for example "Resolve discrepancy
            first" (server default ``false``).
        held_reason: Why the item was held; ``None`` when it was not.
        required_datasets: Dataset names the rule read, from which the item's ``stale`` and
            ``incomplete`` flags derive (server default ``'{}'::text[]``).
        decision_subject: The campaign or budget a decision on this item would write, as
            ``{type, id}``; ``None`` for items without a decision affordance.
    """

    __tablename__ = "recommendation"

    run_id: Mapped[int] = mapped_column(
        sa.ForeignKey("analysis_run.id", ondelete="CASCADE"),
    )
    section: Mapped[str]
    group: Mapped[str]
    rule_id: Mapped[str]
    entity_type: Mapped[str | None]
    entity_id: Mapped[str | None]
    rank: Mapped[int]
    action: Mapped[str]
    reason: Mapped[str]
    evidence: Mapped[dict[str, Any]]
    flags: Mapped[list[str]] = mapped_column(server_default=sa.text("'{}'::text[]"))
    data_status: Mapped[str]
    held: Mapped[bool] = mapped_column(server_default=sa.text("false"))
    held_reason: Mapped[str | None]
    required_datasets: Mapped[list[str]] = mapped_column(server_default=sa.text("'{}'::text[]"))
    decision_subject: Mapped[dict[str, Any] | None]

    __table_args__ = (
        check_in("section", RECOMMENDATION_SECTIONS),
        check_in("data_status", DATA_STATUSES),
        sa.Index("ix_recommendation_run_section_rank", "run_id", "section", "rank"),
    )


class RunFinding(IdPkMixin, _RunEntityKeysMixin, Base):
    """A non-action observation a rule emitted during a run, frozen with the run.

    AAP 0.5.2 (``run_finding``) and 0.5.3 (``Finding``). Codes include ``no_crm_link``,
    ``search_term_coverage``, ``k1_no_baseline``, ``segment_window_unavailable``,
    ``pipeline_unavailable``, ``skipped_stage``, ``conversion_tracking_absent`` and
    ``incomplete_dataset``. Stored-only reads return findings as recorded, without re-running
    rules. ``run_id`` is indexed for the page read and the cascade from :class:`AnalysisRun`.

    Attributes:
        run_id: The :class:`AnalysisRun` that recorded the finding.
        section: The section the finding is shown in (:data:`RECOMMENDATION_SECTIONS`).
        code: The finding code.
        entity: The entity the finding is about, denormalised (for example
            ``{source, type, id, name}``); ``None`` for a section- or account-wide finding.
        message: The rendered message.
        evidence: The numbers behind the message (server default ``'{}'::jsonb``).
    """

    __tablename__ = "run_finding"

    run_id: Mapped[int] = mapped_column(
        sa.ForeignKey("analysis_run.id", ondelete="CASCADE"),
        index=True,
    )
    section: Mapped[str]
    code: Mapped[str]
    entity: Mapped[dict[str, Any] | None]
    message: Mapped[str]
    evidence: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (check_in("section", RECOMMENDATION_SECTIONS),)


class RunWithheld(IdPkMixin, _RunEntityKeysMixin, Base):
    """A pattern withheld for sample size or validity, with its observed and required sizes.

    AAP 0.5.2 (``run_withheld``), 0.5.3 (``Withheld``) and 0.5.4.2 (P6 sample-size
    withholding). Withheld patterns never become items; each section lists them with the
    sample it saw against the minimum it needed, for example "spending days 3 of 5 required"
    or "3 weekend days of 4 required". ``run_id`` is indexed for the page read and the
    cascade from :class:`AnalysisRun`.

    Attributes:
        run_id: The :class:`AnalysisRun` that recorded the withheld pattern.
        section: The section the pattern is listed in (:data:`RECOMMENDATION_SECTIONS`).
        rule_id: The rule whose pattern was withheld, for example ``B4``.
        entity: The entity the pattern is about, denormalised; ``None`` for an account-wide
            withholding such as K1 without a usable baseline.
        reason: Why the pattern was withheld.
        observed: The sample observed, in ``unit``; ``None`` when no sample applies.
        required: The minimum sample required, in ``unit``; ``None`` when no sample applies.
        unit: The sample's unit, for example ``clicks`` or ``days``; ``None`` when no sample
            applies.
    """

    __tablename__ = "run_withheld"

    run_id: Mapped[int] = mapped_column(
        sa.ForeignKey("analysis_run.id", ondelete="CASCADE"),
        index=True,
    )
    section: Mapped[str]
    rule_id: Mapped[str]
    entity: Mapped[dict[str, Any] | None]
    reason: Mapped[str]
    observed: Mapped[Decimal | None]
    required: Mapped[Decimal | None]
    unit: Mapped[str | None]

    __table_args__ = (check_in("section", RECOMMENDATION_SECTIONS),)


class RunSuppression(IdPkMixin, Base):
    """How many items one reason suppressed in one section of a run.

    AAP 0.5.2 (``run_suppression``) and 0.5.3 ("Where outputs are frozen"). The counts cover
    feedback-hidden items, G1-blocked increases and P2 suppressions; each section shows them
    as ``suppressed: {reason: count}``. There is one row per (run, section, reason), enforced
    by ``uq_run_suppression_run_section_reason``, whose index leads with ``run_id`` and so
    also serves the page read and the cascade from :class:`AnalysisRun`.

    Attributes:
        run_id: The :class:`AnalysisRun` that recorded the count.
        section: The section the suppressed items belonged to
            (:data:`RECOMMENDATION_SECTIONS`).
        reason: The suppression reason, for example ``dismiss`` or ``g1_blocked``.
        count: How many items the reason suppressed.
    """

    __tablename__ = "run_suppression"

    run_id: Mapped[int] = mapped_column(
        sa.ForeignKey("analysis_run.id", ondelete="CASCADE"),
    )
    section: Mapped[str]
    reason: Mapped[str]
    count: Mapped[int]

    __table_args__ = (
        check_in("section", RECOMMENDATION_SECTIONS),
        sa.UniqueConstraint(
            "run_id",
            "section",
            "reason",
            name="uq_run_suppression_run_section_reason",
        ),
    )


class RecommendationFeedback(IdPkMixin, Base):
    """A dismiss, snooze or done on an item, suppressing its rule and entity until ``until``.

    AAP 0.5.2 (``recommendation_feedback``), 0.5.3 (``POST /recommendations/{id}/feedback``)
    and 0.5.4.2 (feedback suppression). Feedback suppresses the same rule and entity in later
    runs until ``until``: dismiss 30 days, snooze 7 days, done 30 days. The runner applies it
    before ranking and the overlay applies it live. C0 discrepancy items are exempt; the API
    rejects feedback on them with 422 and on a historical item with 409 ``subject_retired``.

    ``rule_id``, ``entity_type`` and ``entity_id`` are copied from the recommendation, so the
    match across runs needs no join and survives a purge that nulls the recommendation's
    typed entity keys. Two indexes serve the two reads. The overlay looks up the exact
    ``(rule_id, entity_type, entity_id)`` keys of one page, with ``until`` after the read time,
    through ``ix_recommendation_feedback_rule_entity``. The runner reads every row still in
    force before it knows its items, a range over ``until`` alone, through
    ``ix_recommendation_feedback_until``; expired rows stay as history and outside that range.

    Attributes:
        recommendation_id: The :class:`Recommendation` the feedback was given on; the only
            foreign key.
        rule_id: The item's rule, copied from the recommendation.
        entity_type: The item's entity type, copied from the recommendation; ``None`` when
            the item has none.
        entity_id: The item's stable external entity key ``{source}:{type}:{external_id}``
            (``text``), copied from :attr:`Recommendation.entity_id`, never one of its
            internal-id typed keys; no foreign key. ``None`` when the item has none, and a
            ``NULL`` matches only an item whose own key is ``NULL``.
        kind: ``dismiss``, ``snooze`` or ``done`` (:data:`FEEDBACK_KINDS`).
        until: When the suppression ends.
        note: The user's note, if any.
        created_at: When the feedback was given (server default ``now()``).
        created_by: The email of the user who gave it, if known.
    """

    __tablename__ = "recommendation_feedback"

    # Recommendations are deleted only with their run, which normal operation never does, and
    # feedback is looked up by rule and entity or by expiry, so the CASCADE action needs no
    # index here.
    recommendation_id: Mapped[int] = mapped_column(
        sa.ForeignKey("recommendation.id", ondelete="CASCADE"),
    )
    rule_id: Mapped[str]
    entity_type: Mapped[str | None]
    entity_id: Mapped[str | None]
    kind: Mapped[str]
    until: Mapped[datetime]
    note: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=sa.text("now()"))
    created_by: Mapped[str | None]

    __table_args__ = (
        check_in("kind", FEEDBACK_KINDS),
        sa.Index(
            "ix_recommendation_feedback_rule_entity",
            "rule_id",
            "entity_type",
            "entity_id",
            "until",
        ),
        sa.Index("ix_recommendation_feedback_until", "until"),
    )
