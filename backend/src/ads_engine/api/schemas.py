"""Pydantic request and response contract of the ``/api/v1`` REST API (AAP 0.5.3).

Every route in ``ads_engine.api.routes`` declares its request body and ``response_model`` from
this module, and ``web/src/api/types.ts`` mirrors it field for field, so the names and nesting
here are the wire contract. The groups are:

* **Shared types**: :data:`Money`, :data:`MoneyIn` (a request amount: a plain decimal
  string or a whole integer), :data:`MetricValue`, :data:`SampleSize` (a withheld sample
  size), :data:`PositiveBigInt` (strict request ids bounded by :data:`BIGINT_MAX`),
  :data:`StorableText` and :data:`StorableNote` (request text PostgreSQL can store),
  :data:`CalendarDate` (a request date, exactly ``YYYY-MM-DD``) and the ``Literal``
  vocabularies (:data:`SourceName`,
  :data:`SectionKey`, :data:`FlagCode` …), each declared once.
* **Errors**: :class:`ErrorResponse` (``{"error": {"code", "message"}}``) and
  :data:`ERROR_RESPONSES` for the routes' ``responses=``.
* **Auth and runs**: :class:`LoginRequest`, :class:`Me`, :class:`Run`,
  :class:`CreateRunRequest`.
* **Recommendations page**: :class:`RecommendationsPage` with its sections, groups, items,
  frozen :class:`Evidence` and the read-time :class:`ItemOverlay` and :class:`Overlay`;
  :class:`FeedbackRequest` and :class:`Feedback`.
* **Decisions and discrepancies**: :class:`DecisionRequest` (a patch, AAP 0.5.2),
  :class:`ResolveRequest`, :class:`Decision` and :class:`Discrepancy`.
* **Sources, config and health**: :class:`Source`, :class:`SyncRequestView`,
  :class:`ConfigView` and :class:`Health`.

Conventions:

* Money is ``Decimal`` and serialises as a decimal string (``"12.340000"``), never a float,
  so amounts keep their exact precision. Amounts are never summed across currencies.
* Responses (:class:`ApiModel`) ignore unknown keys: frozen ``jsonb`` evidence may carry
  rule-specific keys, which travel in :attr:`Evidence.extra`. Requests (:class:`RequestModel`)
  forbid unknown keys, so a misspelt field is a 422 rather than a silent no-op.
* In a response every model field is always present: a field typed ``X | None``, with or
  without a ``None`` default, may be ``null`` but is never omitted, and the published response
  schema lists every model field as required. A typed-dict view of stored ``jsonb`` is the one
  exception: it returns exactly the keys the stored object holds, each optional. In a request
  a field with a default may be omitted.
* A missing number is ``null`` ("unavailable"), never ``0`` (AAP 0.11.1).

The ``Literal`` vocabularies duplicate the tuples the database ``CHECK`` constraints are built
from. :func:`_check_vocabularies` compares every pair once at import and raises
:class:`RuntimeError` on drift, so the API can never accept or emit a value the schema rejects.

Purity: models and pure mapping helpers (``from_row``) only. No I/O, no database query, no
settings read; the module imports nothing from ``connectors``, ``sync``, ``simulation`` or
``api.routes``.

Example:
    >>> DecisionPatchIn.model_validate({"daily_budget": "120.50"}).present_fields()
    {'daily_budget': Decimal('120.50')}
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self, TypedDict, get_args

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    computed_field,
    field_validator,
    model_validator,
    with_config,
)
from pydantic.config import JsonDict

from ads_engine.analysis.context import (
    DATA_STATUS_VALUES,
    FINDING_CODES,
    FLAG_CODES,
    SECTION_ORDER,
    TONES,
)
from ads_engine.analysis.thresholds import BindingMode as ConfigBindingMode
from ads_engine.analysis.thresholds import (
    ContentAsset,
    CrmBindings,
    GoogleCrmBinding,
    HubSpotMapping,
    ResolvedBinding,
    StageBinding,
    Thresholds,
)
from ads_engine.db.models.accounts import CAMPAIGN_STATUSES
from ads_engine.db.models.analysis import (
    DATA_STATUSES,
    FEEDBACK_KINDS,
    RECOMMENDATION_SECTIONS,
    RUN_KINDS,
    RUN_STATUSES,
    RUN_TRIGGERS,
    AnalysisRun,
    RecommendationFeedback,
    RunFinding,
    RunWithheld,
)
from ads_engine.db.models.decisions import (
    DECISION_ORIGINS,
    DECISION_SHAPES,
    DISCREPANCY_KINDS,
    DISCREPANCY_RESOLUTIONS,
    DISCREPANCY_STATES,
    SUBJECT_TYPES,
)
from ads_engine.db.models.decisions import Decision as DecisionRow
from ads_engine.db.models.decisions import Discrepancy as DiscrepancyRow
from ads_engine.db.models.ops import (
    COMPLETENESS,
    CONNECTION_STATUSES,
    DATASET_ROLES,
    DATASET_STATES,
    MODES,
    SOURCES,
    SYNC_RUN_STATUSES,
    DatasetFreshness,
    SourceDataset,
    SyncRequest,
    SyncRun,
)

__all__ = [
    "BIGINT_MAX",
    "ERROR_RESPONSES",
    "NOTE_MAX_LENGTH",
    "ApiModel",
    "Banner",
    "BannerTone",
    "Baseline",
    "BindingMode",
    "CalendarDate",
    "CampaignStatus",
    "Completeness",
    "ConfigView",
    "ConnectionStatus",
    "ContentAssetView",
    "CreateRunRequest",
    "CrmBindingsView",
    "DataModes",
    "DataStatus",
    "DatasetCoverage",
    "DatasetFreshnessView",
    "DatasetRole",
    "DatasetState",
    "DatasetView",
    "Decision",
    "DecisionOrigin",
    "DecisionPatchIn",
    "DecisionPatchView",
    "DecisionRequest",
    "DecisionShape",
    "DecisionSubjectRef",
    "Discrepancy",
    "DiscrepancyKind",
    "DiscrepancyState",
    "DiscrepancyValue",
    "EntityView",
    "ErrorDetail",
    "ErrorResponse",
    "Evidence",
    "EvidenceWindow",
    "Feedback",
    "FeedbackKind",
    "FeedbackRequest",
    "Finding",
    "FindingCode",
    "Flag",
    "FlagCode",
    "GoogleCrmBindingView",
    "Group",
    "Health",
    "HubSpotMappingView",
    "IntendedStateIn",
    "Item",
    "ItemOverlay",
    "LoginRequest",
    "Me",
    "Metric",
    "MetricValue",
    "Mode",
    "Money",
    "MoneyIn",
    "Overlay",
    "PositiveBigInt",
    "Projection",
    "ProvenanceEntry",
    "RecommendationsPage",
    "RequestModel",
    "ResolutionKey",
    "ResolveRequest",
    "ResolvedBindingView",
    "Run",
    "RunKind",
    "RunStatus",
    "RunTrigger",
    "Sample",
    "SampleSize",
    "Section",
    "SectionKey",
    "Source",
    "SourceDay",
    "SourceModes",
    "SourceName",
    "StageBindingView",
    "StatTest",
    "StorableNote",
    "StorableText",
    "SubjectIn",
    "SubjectType",
    "SubjectView",
    "SyncRequestView",
    "SyncRunStatus",
    "SyncRunView",
    "WindowConfig",
    "Withheld",
    "WithheldSample",
]

# ---------------------------------------------------------------------------
# Shared scalar types
# ---------------------------------------------------------------------------


def _money_input(value: object) -> object:
    """Refuse JSON numbers with a fraction (``float``) and booleans as money input.

    A float has already lost decimal precision by the time it reaches the API (``0.1`` is not
    exactly one tenth), so amounts must arrive as decimal strings or whole integers.

    Raises:
        ValueError: When ``value`` is a ``float`` or a ``bool``.
    """
    if isinstance(value, bool | float):
        raise ValueError('send amounts as decimal strings such as "120.50", not JSON numbers')
    return value


def _money_output(value: Decimal) -> str:
    """Render an amount as fixed-point text, never exponent notation.

    ``str(Decimal("1E+2"))`` is ``"1E+2"``; an amount submitted that way and returned from a
    just-written row before the database normalises it must still reach the client as
    ``"100"``, the form frozen evidence and the decision patch use.
    """
    return format(value, "f")


_MONEY_PATTERN: Final = r"^[0-9]+(?:\.[0-9]+)?$"
"""The exact lexical form of a request amount string: a plain positional decimal.

ASCII digits, then optionally a ``.`` and at least one more ASCII digit. No sign, whitespace,
exponent, leading or trailing ``.``, or non-ASCII digit (``"١٢٠"``). :data:`MoneyIn` enforces it
and :data:`_MONEY_INPUT_SCHEMA` publishes it, so the two can never disagree. It states the form
only: the value rules (positive, below 10^12, at most 6 decimal places once trailing zeros
are dropped, the currency's minor unit) keep their own, more specific 422 answers.
"""

_MONEY_RE: Final = re.compile(_MONEY_PATTERN)
""":data:`_MONEY_PATTERN`, compiled once for :func:`_money_lexical_form`."""


def _money_lexical_form(value: object) -> object:
    """Refuse a request amount string that is not a plain decimal (:data:`_MONEY_PATTERN`).

    Pydantic's ``Decimal`` parsing is lax: it strips whitespace and takes a sign, an exponent
    (``"1e3"``, ``"4.0E+2"``) and non-ASCII digits, none of which the published contract
    allows. The whole string must match (``re.fullmatch``), so a trailing newline is refused
    too. Every other value (an ``int``, a ``Decimal`` from a Python caller, ``None``) passes
    through to :data:`Money`'s own checks, which still refuse a ``float`` or a ``bool``.

    The message never repeats the value: the API renders validation failures as
    ``loc: msg``.

    Raises:
        ValueError: When ``value`` is a ``str`` that is not a plain decimal.
    """
    if isinstance(value, str) and _MONEY_RE.fullmatch(value) is None:
        raise ValueError(
            'send amounts as plain decimal strings such as "120.50": digits with an optional '
            "fraction, and no sign, spaces or exponent"
        )
    return value


_MONEY_INPUT_SCHEMA: Final[JsonDict] = {
    "anyOf": [
        {"type": "string", "pattern": _MONEY_PATTERN},
        {"type": "integer", "minimum": 1, "maximum": 999_999_999_999},
    ],
    "description": (
        'An exact amount: a plain decimal string such as "120.50", or a whole JSON integer. '
        "The string pattern is the exact form the server accepts: ASCII digits with an "
        "optional fraction, and no sign, spaces or exponent. A JSON number written with a "
        "fraction or a decimal point (120.5, 120.0) is refused. The server then checks the "
        "value: positive, below 10^12, at most 6 decimal places once trailing zeros are "
        "dropped, and no finer than the currency's minor unit (Google: a whole number of "
        "micros), and answers 422 otherwise."
    ),
}
"""The request (validation-mode) JSON schema of :data:`Money`: what a client may send.

Pydantic would publish an unrestricted JSON ``number`` that :func:`_money_input` refuses, plus
the non-standard ``max_digits``/``decimal_places`` keys. This schema offers the two forms
:data:`MoneyIn` accepts: a string of exactly :data:`_MONEY_PATTERN`, or a whole integer from 1
to 10^12 - 1. The pattern is lexical only; the value rules are stated in the description and
answered with their own 422 codes (``ads_engine.decisions``' ``invalid_amount`` and
``sub_minor_unit``, Pydantic's digit limits), so a string such as ``"0"`` or ``"12.3400000"``
matches it and is judged on its value.
"""


Money = Annotated[
    Decimal,
    BeforeValidator(_money_input),
    Field(max_digits=18, decimal_places=6, allow_inf_nan=False),
    PlainSerializer(_money_output, return_type=str, when_used="json"),
    WithJsonSchema(_MONEY_INPUT_SCHEMA, mode="validation"),
]
"""An exact amount in one account's currency, matching the ``numeric(18,6)`` columns.

Serialised as a fixed-point decimal string (``"120.500000"``), never in exponent notation,
and published as a string in response schemas.
A JSON number with a fraction is refused, because it has already lost precision; any string
``Decimal`` parses is taken, so response models read stored amounts as they are. Request
fields use :data:`MoneyIn`, which also holds strings to :data:`_MONEY_PATTERN`, and publish
:data:`_MONEY_INPUT_SCHEMA`.
Validation to the currency's minor unit is ``ads_engine.decisions``' job (AAP 0.5.2).
"""

MoneyIn = Annotated[Money, BeforeValidator(_money_lexical_form)]
"""A request amount: :data:`Money` whose strings must be plain decimals (:data:`_MONEY_PATTERN`).

Clients send a plain decimal string (``"120.50"``) or a whole integer. A string with a sign,
whitespace, an exponent or a non-ASCII digit is refused with 422, exactly as the published
pattern says. The lexical check runs first (Pydantic runs the last-declared before-validator
first), then :data:`Money`'s ``float``/``bool`` refusal, ``Decimal`` parsing and digit
limits. Response models keep :data:`Money`, so stored amounts are never re-judged.
"""


def _metric_input(value: object) -> object:
    """Render a ``Decimal`` metric value as the fixed-point string the runner stores.

    Frozen evidence holds Decimals as strings (``analysis.context.json_value``); values read
    from ``numeric`` columns, such as ``run_withheld.observed``, arrive as ``Decimal`` and are
    rendered the same way, so one value type reaches the client.

    Raises:
        ValueError: When the ``Decimal`` is not finite.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"metric values must be finite, found {value}")
        return format(value, "f")
    return value


MetricValue = Annotated[str | int | float | bool | None, BeforeValidator(_metric_input)]
"""A value copied from frozen evidence ``jsonb``: decimal strings for exact numbers (money,
rates, shares), integers for counts, a pre-rendered string such as ``"n/a"``, or ``null`` for
unavailable (never ``0``)."""


def _sample_size_input(value: object) -> object:
    """Render a ``numeric`` sample size in the type item evidence gives the same size.

    ``run_withheld.observed`` and ``required`` are ``numeric(18,6)`` columns, so a count of
    one reads back as ``Decimal("1.000000")``, while ``evidence.sample`` carries counts as
    integers. A whole-number ``Decimal`` therefore becomes an ``int`` (``Decimal("28.000000")``
    → ``28``) and a fractional one keeps its exact fixed-point string (``Decimal("2.900000")``
    → ``"2.900000"``), as with fractional expected outcomes or conversions. Any other value
    passes through unchanged.

    Raises:
        ValueError: When the ``Decimal`` is not finite.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"sample sizes must be finite, found {value}")
        if value == value.to_integral_value():
            return int(value)
        return format(value, "f")
    return value


SampleSize = Annotated[int | str | None, BeforeValidator(_sample_size_input)]
"""A withheld pattern's observed or required size: an integer for a whole-number size, a
fixed-point decimal string for a fractional one, or ``null`` when no sample applies (never
``0``)."""

BIGINT_MAX: Final = 2**63 - 1
"""The largest value a PostgreSQL ``bigint`` holds, the type of every internal id column.

Request ids are bounded by it, through :data:`PositiveBigInt` in bodies and
``Query(le=BIGINT_MAX)`` on routes, so an id the database cannot represent is a 422
validation error, never a ``bigint out of range`` database error."""

PositiveBigInt = Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]
"""An internal id a request names: a positive integer of at most :data:`BIGINT_MAX`.

Strict, as the published ``integer`` schema says: only a real JSON integer is taken, so a
string (``"1"``), a boolean (``true``) or a number with a decimal point (``1.0``) is refused
with 422 rather than coerced. A larger value is refused with 422 before any SQL runs,
because it could never match a ``bigint`` primary key and binding it would fail in the
database instead."""


def _utf8_encodable(value: str) -> bool:
    """Whether UTF-8 can encode ``value``; an unpaired surrogate such as U+D800 cannot."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _storable_text(value: str) -> str:
    """Return ``value`` unchanged when a PostgreSQL ``text`` column can store it.

    PostgreSQL ``text`` cannot hold U+0000, and psycopg sends every string as UTF-8, which
    an unpaired surrogate cannot be encoded in. Either would fail in the driver or the
    server after the request was accepted, so it is refused here, as a 422. Pydantic's
    ``str`` validation already refuses unpaired surrogates (``string_unicode``); the check
    keeps this validator's guarantee whatever ``str`` schema runs before it.

    The message names the problem, never the value, which may be an email. The error is
    raised outside any ``except`` block, so it chains no ``UnicodeEncodeError`` carrying
    the string.

    Raises:
        ValueError: When ``value`` contains U+0000 or a code point UTF-8 cannot encode.
    """
    if "\x00" in value:
        raise ValueError("must not contain a NUL (U+0000) character")
    if not _utf8_encodable(value):
        raise ValueError("must be valid Unicode text, without unpaired surrogates")
    return value


StorableText = Annotated[str, AfterValidator(_storable_text)]
"""Request text bound for a PostgreSQL ``text`` column or comparison.

A value holding U+0000 or a code point UTF-8 cannot encode is refused with 422 before any
SQL runs; every other value, non-ASCII included, passes unchanged. Length bounds stay on
each field."""

_CALENDAR_DATE_FORM: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
"""The one text form a request date may take, matched in full: ``YYYY-MM-DD`` in ASCII digits,
with no whitespace, time or zone (RFC 3339 ``full-date``, the JSON schema ``date`` format)."""

_CALENDAR_DATE_MESSAGE: Final = (
    'send a calendar date as a "YYYY-MM-DD" string, such as "2026-09-01"'
)
"""The 422 message of a refused request date; it names the expected form, never the value."""


def _calendar_date_input(value: object) -> object:
    """Admit only a ``YYYY-MM-DD`` string, or a ``date`` that is not a ``datetime``.

    Pydantic's lax ``date`` validation turns a UNIX timestamp (``1679616000``, as a number or
    a numeric string) and a midnight datetime string (``"2023-03-24T00:00:00"``, with or
    without a zone) into a date, so a client sending an instant would get a window over days
    it never named. FastAPI validates a parsed JSON body in Python mode, where strict ``date``
    validation would refuse every string, so the form is checked here instead: a string must
    match :data:`_CALENDAR_DATE_FORM` in full and is then parsed by Pydantic, which still
    refuses an impossible date such as ``"2026-02-30"``. A ``date`` passes for Python callers;
    a ``datetime``, although a ``date`` subclass, is an instant and is refused.

    Raises:
        ValueError: When ``value`` is neither such a string nor a plain ``date``: a number, a
            boolean, bytes, a ``datetime``, or a string in any other form.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _CALENDAR_DATE_FORM.fullmatch(value) is not None:
        return value
    raise ValueError(_CALENDAR_DATE_MESSAGE)


CalendarDate = Annotated[date, BeforeValidator(_calendar_date_input)]
"""A calendar date a request names: exactly ``YYYY-MM-DD`` on the wire.

Published as ``{"type": "string", "format": "date"}``, the one form :func:`_calendar_date_input`
admits. A timestamp, a datetime string or any other spelling is a 422 rather than being
converted to a day the client never named. Response dates stay plain ``date``: they are
outputs, always rendered in this form."""

# ---------------------------------------------------------------------------
# Vocabularies (each equal to its database tuple; checked at import)
# ---------------------------------------------------------------------------

SourceName = Literal["google", "linkedin", "hubspot"]
"""An external data source (``db.models.ops.SOURCES``)."""

Mode = Literal["live", "simulated"]
"""Whether a dataset holds real platform data or seeded mock data (``MODES``)."""

DatasetRole = Literal["primary", "companion"]
"""A source's own dataset, or the simulated CRM companion of a LinkedIn one."""

DatasetState = Literal["building", "active", "retired"]
"""Dataset lifecycle (``DATASET_STATES``)."""

Completeness = Literal["complete", "incomplete", "unknown"]
"""Completeness of a dataset over its covered range (``COMPLETENESS``)."""

ConnectionStatus = Literal["ok", "stale", "denied", "error"]
"""Health of a source connection (``CONNECTION_STATUSES``)."""

SyncRunStatus = Literal["running", "succeeded", "failed"]
"""Outcome of one sync job run (``SYNC_RUN_STATUSES``)."""

SectionKey = Literal["keywords", "campaigns", "budget", "creative", "white_papers"]
"""The five sections, in the fixed order the page returns them (``RECOMMENDATION_SECTIONS``)."""

RunKind = Literal["default", "custom"]
"""``default``: the worker's run of the configured window; ``custom``: any explicit window."""

RunTrigger = Literal["scheduled", "event", "manual"]
"""What started a run (``RUN_TRIGGERS``)."""

RunStatus = Literal["running", "succeeded", "failed"]
"""Run lifecycle (``RUN_STATUSES``)."""

DataStatus = Literal["live", "simulated", "stale", "mixed"]
"""An item's data status; ``mixed`` marks a join of live and simulated datasets."""

FeedbackKind = Literal["dismiss", "snooze", "done"]
"""Feedback on an item (``FEEDBACK_KINDS``)."""

SubjectType = Literal["campaign", "budget"]
"""What a decision or discrepancy is about (``SUBJECT_TYPES``)."""

DecisionShape = Literal["own_budget", "shared_member", "budget"]
"""The decision shape fixed by the subject's current state (AAP 0.5.2)."""

DecisionOrigin = Literal["baseline", "user", "recommendation", "resolution"]
"""Who wrote a decision row (``DECISION_ORIGINS``)."""

CampaignStatus = Literal["active", "paused", "ended"]
"""A campaign's normalised status (``CAMPAIGN_STATUSES``)."""

DiscrepancyKind = Literal["status", "budget", "untracked", "missing"]
"""The C0 discrepancy kinds (AAP 0.5.4.4)."""

DiscrepancyState = Literal["open", "awaiting_platform", "resolved"]
"""Discrepancy lifecycle (``DISCREPANCY_STATES``)."""

ResolutionKey = Literal[
    "platform_correct",
    "decision_correct",
    "adopt_platform",
    "record_intended",
    "acknowledge_removal",
]
"""The resolution API keys (``DISCREPANCY_RESOLUTIONS``, AAP 0.5.4.4)."""

BannerTone = Literal["neutral", "info", "warning", "critical", "success"]
"""A banner or badge tone (AAP 0.4.2 ``Badge``/``Banner``; ``analysis.context.TONES``)."""

FlagCode = Literal[
    "simulated",
    "stale",
    "incomplete",
    "short_window",
    "unverified_discrepancies",
    "attribution_floor",
    "creative_floor",
    "deal_floor",
    "unknown_crm_evidence",
    "meeting_time_proxy",
    "pipeline_unavailable",
    "no_crm_link",
    "not_distinguishable",
    "search_term_coverage",
    "shared_budget",
]
"""Every AAP 0.5.3 ``Flag`` code (``analysis.context.FLAG_CODES``)."""

FindingCode = Literal[
    "no_crm_link",
    "search_term_coverage",
    "k1_no_baseline",
    "segment_window_unavailable",
    "pipeline_unavailable",
    "skipped_stage",
    "conversion_tracking_absent",
    "incomplete_dataset",
]
"""Every AAP 0.5.3 ``Finding`` code (``analysis.context.FINDING_CODES``)."""

BindingMode = Literal["stage_label", "property"]
"""How a CRM count is bound in ``config/hubspot_mapping.yaml``: a lifecycle stage label, or a
contact date property (AAP 0.5.4.7; ``analysis.thresholds.BindingMode``)."""

NOTE_MAX_LENGTH: Final = 2000
"""The longest free-text note a decision, resolution or feedback request may carry."""

StorableNote = Annotated[str, Field(max_length=NOTE_MAX_LENGTH), AfterValidator(_storable_text)]
"""A free-text note: at most :data:`NOTE_MAX_LENGTH` characters of :data:`StorableText`.

The length bound belongs to the ``str`` schema and runs first, so an overlong note is
``string_too_long`` whatever it contains."""


def _check_vocabularies() -> None:
    """Fail at import when a ``Literal`` vocabulary drifts from the tuple it mirrors.

    Every alias must hold exactly the values of its database or analysis tuple, and
    :data:`SectionKey` must also keep the order of ``analysis.context.SECTION_ORDER``, which
    is the order the page returns sections in. Written as ``if … raise`` so the check also
    runs under ``python -O``.

    Raises:
        RuntimeError: Naming the alias, the tuple and both value sets on the first mismatch.
    """
    pairs: tuple[tuple[str, object, Sequence[str], str], ...] = (
        ("SourceName", SourceName, SOURCES, "db.models.ops.SOURCES"),
        ("Mode", Mode, MODES, "db.models.ops.MODES"),
        ("DatasetState", DatasetState, DATASET_STATES, "db.models.ops.DATASET_STATES"),
        ("DatasetRole", DatasetRole, DATASET_ROLES, "db.models.ops.DATASET_ROLES"),
        ("Completeness", Completeness, COMPLETENESS, "db.models.ops.COMPLETENESS"),
        (
            "ConnectionStatus",
            ConnectionStatus,
            CONNECTION_STATUSES,
            "db.models.ops.CONNECTION_STATUSES",
        ),
        ("SyncRunStatus", SyncRunStatus, SYNC_RUN_STATUSES, "db.models.ops.SYNC_RUN_STATUSES"),
        (
            "SectionKey",
            SectionKey,
            RECOMMENDATION_SECTIONS,
            "db.models.analysis.RECOMMENDATION_SECTIONS",
        ),
        ("RunKind", RunKind, RUN_KINDS, "db.models.analysis.RUN_KINDS"),
        ("RunTrigger", RunTrigger, RUN_TRIGGERS, "db.models.analysis.RUN_TRIGGERS"),
        ("RunStatus", RunStatus, RUN_STATUSES, "db.models.analysis.RUN_STATUSES"),
        ("FeedbackKind", FeedbackKind, FEEDBACK_KINDS, "db.models.analysis.FEEDBACK_KINDS"),
        ("DataStatus", DataStatus, DATA_STATUSES, "db.models.analysis.DATA_STATUSES"),
        ("DataStatus", DataStatus, DATA_STATUS_VALUES, "analysis.context.DATA_STATUS_VALUES"),
        ("SubjectType", SubjectType, SUBJECT_TYPES, "db.models.decisions.SUBJECT_TYPES"),
        ("DecisionShape", DecisionShape, DECISION_SHAPES, "db.models.decisions.DECISION_SHAPES"),
        (
            "DecisionOrigin",
            DecisionOrigin,
            DECISION_ORIGINS,
            "db.models.decisions.DECISION_ORIGINS",
        ),
        (
            "CampaignStatus",
            CampaignStatus,
            CAMPAIGN_STATUSES,
            "db.models.accounts.CAMPAIGN_STATUSES",
        ),
        (
            "DiscrepancyKind",
            DiscrepancyKind,
            DISCREPANCY_KINDS,
            "db.models.decisions.DISCREPANCY_KINDS",
        ),
        (
            "DiscrepancyState",
            DiscrepancyState,
            DISCREPANCY_STATES,
            "db.models.decisions.DISCREPANCY_STATES",
        ),
        (
            "ResolutionKey",
            ResolutionKey,
            DISCREPANCY_RESOLUTIONS,
            "db.models.decisions.DISCREPANCY_RESOLUTIONS",
        ),
        ("BannerTone", BannerTone, TONES, "analysis.context.TONES"),
        ("FlagCode", FlagCode, FLAG_CODES, "analysis.context.FLAG_CODES"),
        ("FindingCode", FindingCode, FINDING_CODES, "analysis.context.FINDING_CODES"),
        (
            "BindingMode",
            BindingMode,
            get_args(ConfigBindingMode.__value__),
            "analysis.thresholds.BindingMode",
        ),
    )
    for alias_name, alias, values, values_name in pairs:
        literal_values = get_args(alias)
        if set(literal_values) != set(values) or len(literal_values) != len(values):
            raise RuntimeError(
                f"api.schemas.{alias_name} {sorted(literal_values)} does not match "
                f"{values_name} {sorted(values)}"
            )
    if get_args(SectionKey) != tuple(SECTION_ORDER):
        raise RuntimeError(
            f"api.schemas.SectionKey order {list(get_args(SectionKey))} does not match "
            f"analysis.context.SECTION_ORDER {list(SECTION_ORDER)}"
        )


_check_vocabularies()

# ---------------------------------------------------------------------------
# Base classes and the error shape
# ---------------------------------------------------------------------------


class ApiModel(BaseModel):
    """Base of every response model.

    Unknown keys are ignored, because frozen ``jsonb`` evidence may gain keys after the
    contract was written; rule-specific data travels in :attr:`Evidence.extra`.
    ``from_attributes`` lets a model validate straight from an ORM row whose attribute names
    match its fields.

    Serialisation always emits every field, a ``None`` default as ``null``, because no route
    excludes unset or default values. ``json_schema_serialization_defaults_required`` makes
    the published response schema say so: every field, defaulted or not, is ``required``.
    It changes the JSON schema only, never what is serialised or validated.
    """

    model_config = ConfigDict(
        extra="ignore",
        from_attributes=True,
        json_schema_serialization_defaults_required=True,
    )


class RequestModel(BaseModel):
    """Base of every request body: an unknown field is a 422, never silently dropped."""

    model_config = ConfigDict(extra="forbid")


class ErrorDetail(ApiModel):
    """The body of an error response.

    Attributes:
        code: Machine-readable code, for example ``subject_retired`` or ``run_in_progress``.
        message: Human-readable explanation.
    """

    code: str
    message: str


class ErrorResponse(ApiModel):
    """Every error the API returns: ``{"error": {"code", "message"}}`` (AAP 0.5.3).

    Attributes:
        error: The error's code and message.
    """

    error: ErrorDetail


ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    401: {"model": ErrorResponse, "description": "Not signed in (no valid session cookie)"},
    403: {"model": ErrorResponse, "description": "Missing X-Requested-With: ads-engine header"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict, for example subject_retired"},
    422: {"model": ErrorResponse, "description": "Validation error"},
    429: {"model": ErrorResponse, "description": "Too many login attempts"},
}
"""The documented error statuses, for every route's ``responses=`` (OpenAPI only)."""

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(RequestModel):
    """``POST /auth/login`` body.

    Attributes:
        email: The admin email, trimmed and lowercased before the length check, so the login
            matches ``app_user.email`` whatever case the user typed. It is compared in SQL,
            so text PostgreSQL cannot hold (:data:`StorableText`) is a 422.
        password: The password exactly as typed: never trimmed or transformed, because every
            character is part of the secret. It is held in memory only and never stored or
            sent to SQL, so it is not :data:`StorableText`.
    """

    email: StorableText = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email", mode="before")
    @classmethod
    def _normalise_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class Me(ApiModel):
    """The signed-in user (``GET /auth/me``, ``POST /auth/login``, ``POST /auth/logout``).

    Attributes:
        email: The admin email.
    """

    email: str


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class SourceDay(ApiModel):
    """One source's date range and native day boundary within a window (AAP 0.5.4.1).

    Attributes:
        source: The day source (Google or LinkedIn; HubSpot holds instants only).
        start: The first native date, inclusive.
        end: The last native date, inclusive.
        boundary: The IANA zone whose midnight ends the source's days, for example
            ``America/New_York`` for Google or ``UTC`` for LinkedIn.
    """

    source: SourceName
    start: date
    end: date
    boundary: str


class _PerSourceModes(ApiModel):
    """The mode of each source; ``None`` where a source has no dataset."""

    google: Mode | None = None
    linkedin: Mode | None = None
    hubspot: Mode | None = None


class DataModes(_PerSourceModes):
    """The mode of each source as a run read it, frozen with the run (``Run.data_modes``).

    Attributes:
        google: Google's mode at compute time; ``None`` when it had no active dataset.
        linkedin: LinkedIn's mode at compute time.
        hubspot: HubSpot's mode at compute time.
    """


def _source_rank(entry: object) -> int:
    """Sort key placing a ``source_days`` element in ``SOURCES`` order (unknown last)."""
    source = entry.get("source") if isinstance(entry, Mapping) else None
    return SOURCES.index(source) if source in SOURCES else len(SOURCES)


class Run(ApiModel):
    """Run metadata (AAP 0.5.3 ``Run``): ``GET /runs/*``, ``POST /runs`` and the page header.

    Attributes:
        id: The ``analysis_run`` id; the UI addresses a custom run as ``/actions?run=<id>``.
        kind: ``default`` or ``custom``.
        window_start: First calendar date of the window.
        window_end: Last calendar date of the window, inclusive.
        days: Days in the window, both ends included.
        source_days: Each day source's range and native day boundary, Google then LinkedIn.
        display_timezone: ``DISPLAY_TIMEZONE``, used only to format instants.
        short_window: Whether ``days`` is below ``trusted_min_days`` (AAP 0.5.4.1).
        default_days: The ``window.default_days`` the run was computed with.
        trusted_min_days: The ``window.trusted_min_days`` the run was computed with.
        trigger: ``scheduled``, ``event`` or ``manual``.
        data_modes: Each source's mode as the run read it.
        config_hash: Hash of the configuration the run used.
        status: ``running``, ``succeeded`` or ``failed``.
        finished_at: When the run finished; ``None`` while running.
    """

    id: int
    kind: RunKind
    window_start: date
    window_end: date
    days: int
    source_days: list[SourceDay]
    display_timezone: str
    short_window: bool
    default_days: int
    trusted_min_days: int
    trigger: RunTrigger
    data_modes: DataModes
    config_hash: str
    status: RunStatus
    finished_at: AwareDatetime | None

    @classmethod
    def from_row(cls, run: AnalysisRun, *, display_timezone: str) -> Self:
        """Build the view of a stored run.

        ``default_days`` and ``trusted_min_days`` are the ``window.*`` values frozen with the
        run when it was computed, and ``short_window`` is ``days < trusted_min_days`` with
        that stored minimum, never a literal and never today's configuration (AAP 0.5.4.1,
        0.5.3 "Where outputs are frozen"). The view therefore agrees with the ``short_window``
        flags frozen in the run's items however ``config/thresholds.yaml`` changes later.

        Args:
            run: The stored run.
            display_timezone: ``DISPLAY_TIMEZONE`` from the settings.

        Returns:
            The run view, with ``source_days`` in ``SOURCES`` order and a ``None`` mode for
            every source absent from ``run.data_modes``.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        days = (run.window_end - run.window_start).days + 1
        trusted_min_days = run.trusted_min_days
        modes: Mapping[str, Any] = run.data_modes or {}
        return cls.model_validate(
            {
                "id": run.id,
                "kind": run.kind,
                "window_start": run.window_start,
                "window_end": run.window_end,
                "days": days,
                "source_days": sorted(run.source_day_boundaries or [], key=_source_rank),
                "display_timezone": display_timezone,
                "short_window": days < trusted_min_days,
                "default_days": run.default_days,
                "trusted_min_days": trusted_min_days,
                "trigger": run.trigger,
                "data_modes": {source: modes.get(source) for source in SOURCES},
                "config_hash": run.config_hash,
                "status": run.status,
                "finished_at": run.finished_at,
            }
        )


class CreateRunRequest(RequestModel):
    """``POST /runs`` body: a custom window of calendar dates applied to each source's days.

    Each date is a JSON string of exactly the form ``YYYY-MM-DD`` (:data:`CalendarDate`), for
    example ``{"window_start": "2026-09-08", "window_end": "2026-10-05"}``. A UNIX timestamp
    (number or numeric string), a datetime string with or without a zone, surrounding
    whitespace, unpadded or undelimited digits, and an impossible date such as
    ``"2026-02-30"`` are each a 422 ``validation_error`` naming the field.

    The span (1 day to ``window.max_days``), the end (no later than the default window end)
    and the start (no earlier than the earliest date the analysis's lookbacks allow) are
    validated by ``AnalysisWindow.custom``, whose ``InvalidWindow`` the app maps to 422, so
    those rules live in one place.

    Attributes:
        window_start: First calendar date, inclusive, as ``YYYY-MM-DD``.
        window_end: Last calendar date, inclusive, as ``YYYY-MM-DD``.
    """

    window_start: CalendarDate
    window_end: CalendarDate


# ---------------------------------------------------------------------------
# Recommendations page: entity, flags and evidence (AAP 0.5.3, 0.5.4.8)
# ---------------------------------------------------------------------------


class Flag(ApiModel):
    """A badge on an item (AAP 0.5.3 ``Flag``).

    Attributes:
        code: The flag code.
        message: The badge's explanatory text, for example "Data as of Oct 1, 2026, 3:10 AM
            UTC".
    """

    code: FlagCode
    message: str


class EntityView(ApiModel):
    """The entity an item, finding or withheld pattern is about: ``{source, type, id, name}``.

    ``id`` is an opaque identifier, always serialised as a string. Items, findings and
    withheld patterns share one namespace: the platform's external id, or the composite key of
    an entity without a table row (``EntityRef.to_json``), so a finding or withheld pattern
    names its entity exactly as that entity's items do. A numeric id recorded by an earlier
    run renders as its decimal string. ``None`` when the stored entity carries no id.

    Attributes:
        source: The entity's source.
        type: The entity type, for example ``campaign``, ``search_term`` or
            ``content_asset``.
        id: The identifier, as a string.
        name: The display name.
    """

    # Entity JSON stored by earlier runs may carry a numeric id; coercion keeps the wire type
    # a single string.
    model_config = ConfigDict(extra="ignore", from_attributes=True, coerce_numbers_to_str=True)

    source: SourceName | None
    type: str | None
    id: str | None
    name: str | None


class EvidenceWindow(ApiModel):
    """The exact window an item was computed over (``Evidence.window``).

    When the sources' day boundaries differ, ``boundaries_differ`` says so, so misaligned
    buckets are never presented as one common day (AAP 0.5.4.1).

    Attributes:
        start: First calendar date.
        end: Last calendar date, inclusive.
        days: Days in the window.
        source_days: Each day source's range and native day boundary.
        boundaries_differ: Whether the sources' day boundaries differ, for example Google on
            ``America/New_York`` and LinkedIn on ``UTC``. ``None`` on evidence frozen by a run
            that predates this key.
        description: Each source's range and boundary as one sentence, for example "Google
            2026-09-04 to 2026-10-01 (America/New_York); LinkedIn same dates (UTC)". ``None``
            on evidence frozen by a run that predates this key.
    """

    start: date
    end: date
    days: int
    source_days: list[SourceDay]
    boundaries_differ: bool | None = None
    description: str | None = None


class Metric(ApiModel):
    """One number an item's reason cites (``Evidence.metrics``).

    Attributes:
        key: Stable machine key, for example ``spend`` or ``cost_per_conversion``.
        label: Display label.
        value: The value; ``None`` means unavailable and renders "unavailable", never 0.
        unit: The unit, for example ``currency``, ``clicks``, ``ratio`` or ``days``.
        currency: ISO currency code for money values.
        floor: Whether the value is a floor, rendered "≥ N" or "≤ X" (AAP 0.5.4.7).
        unavailable: Whether the value is unavailable.
    """

    key: str
    label: str
    value: MetricValue
    unit: str | None
    currency: str | None = None
    floor: bool | None = None
    unavailable: bool | None = None


class Baseline(ApiModel):
    """A value an item's metrics were compared against (``Evidence.baselines``).

    A baseline that no metric of its item shares a unit with carries its own ``unit`` and,
    for money, ``currency``, so it is formatted in the account currency (AAP 0.5.5).

    Attributes:
        key: Stable machine key, for example ``account_cost_per_outcome``.
        label: Display label.
        value: The baseline; ``None`` when unavailable.
        unit: The unit, for example ``currency`` or ``ratio``. ``None`` for a baseline shown
            in the unit of the metric it pairs with, a unitless one, and one frozen by a run
            that predates this key.
        currency: ISO currency code for a money value. ``None`` otherwise, and on evidence
            frozen by a run that predates this key.
    """

    key: str
    label: str
    value: MetricValue
    unit: str | None = None
    currency: str | None = None


class Projection(ApiModel):
    """A labelled projection beside a sample, for example CR3's impressions per variant.

    Attributes:
        value: The projected figure.
        unit: Its unit, for example ``impressions`` or ``days``.
        basis: How it was projected, for example "projected through the observed open rate".
    """

    value: MetricValue
    unit: str
    basis: str


class Sample(ApiModel):
    """A sample size against its minimum, in the unit the rule uses (AAP 0.5.4.8).

    Attributes:
        observed: The sample observed.
        required: The minimum the rule requires.
        unit: The unit, for example ``clicks``, ``impressions``, ``form opens`` or ``days``.
        projected: An optional labelled projection.
    """

    observed: MetricValue
    required: MetricValue
    unit: str
    projected: Projection | None = None


class StatTest(ApiModel):
    """The statistical test behind an item (``Evidence.test``).

    Named ``StatTest`` rather than ``Test…`` so pytest never collects it.

    Attributes:
        name: The test, for example "Fisher's exact test".
        p_value: The raw p-value; ``None`` when it could not be computed.
        p_adjusted: The Holm-adjusted p-value, when a correction applies.
        alpha: The significance level the result is judged at.
    """

    name: str
    p_value: MetricValue
    p_adjusted: MetricValue = None
    alpha: MetricValue


class DatasetCoverage(ApiModel):
    """A dataset an item read, with its completeness and coverage at compute time.

    A coverage date travels with its day boundary, so it is shown labelled with its source's
    day boundary (AAP 0.5.5), HubSpot's included, which ``Evidence.window.source_days`` does
    not list.

    Attributes:
        dataset: The dataset name, for example ``google.search_terms``.
        completeness: ``complete``, ``incomplete`` or ``unknown``.
        covered_through: The last native date the dataset covered; ``None`` when unknown.
        boundary: The IANA zone whose midnight ends ``covered_through``, for example
            ``America/New_York`` for Google or ``UTC`` for LinkedIn and HubSpot. ``None``
            without a ``covered_through``, and on evidence frozen by a run that predates this
            key, whose Google and LinkedIn dates take their boundary from ``source_days``.
    """

    dataset: str
    completeness: Completeness
    covered_through: date | None
    boundary: str | None = None


class ProvenanceEntry(ApiModel):
    """One dataset an item read, as it was when the run computed it (AAP 0.3.2).

    Attributes:
        source: The dataset's source.
        dataset_id: The ``source_dataset`` id.
        mode: ``live`` or ``simulated`` at compute time.
        role: ``primary``, or ``companion`` for the simulated CRM of a simulated LinkedIn
            dataset.
    """

    source: SourceName
    dataset_id: int
    mode: Mode
    role: DatasetRole


class Evidence(ApiModel):
    """Everything an item's conclusion rests on, frozen with the run (AAP 0.5.4.8).

    ``provenance`` is frozen at compute time and never replaced by the current source
    status; the current status travels separately in :attr:`ItemOverlay.data_status_now`.
    White-paper items carry their per-creative rows in ``extra.rows``; expansion items carry
    their adjacent terms in ``extra.adjacent_terms``. Every card names its headline number in
    ``extra.headline_metric``, the ``key`` of one of its own ``metrics``; white-paper rows
    name none.

    Attributes:
        window: The exact window and each source's day boundary.
        metrics: Every number the reason cites, with units and currency.
        baselines: The values those metrics were compared against.
        sample: The sample size against its minimum, when the rule is sample-gated.
        test: The statistical test, when one was used.
        thresholds: ``{dotted key: configured value}`` for every threshold applied.
        datasets: The datasets read, with completeness and coverage.
        provenance: Each dataset read, with its id, mode and role at compute time.
        extra: Rule-specific detail.
    """

    window: EvidenceWindow
    metrics: list[Metric]
    baselines: list[Baseline]
    sample: Sample | None = None
    test: StatTest | None = None
    thresholds: dict[str, Any]
    datasets: list[DatasetCoverage]
    provenance: list[ProvenanceEntry]
    extra: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Recommendations page: items, sections and the overlay
# ---------------------------------------------------------------------------


class ItemOverlay(ApiModel):
    """An item's current state, computed at read time and kept apart from frozen evidence.

    AAP 0.3.2 "Refresh after change, and the current-state overlay". When ``historical`` is
    true, a dataset the item read is no longer active: every action on the card is disabled.

    Attributes:
        held_now: Whether the item is held now, for any of three causes: it is
            ``historical`` (a dataset it read is no longer active, or its provenance cannot
            be verified); it was ``held`` at compute time, a frozen hold the overlay never
            lifts; or, for every rule but C0, a subject it depends on has an ``open`` or
            ``awaiting_platform`` discrepancy now, including one detected after the run.
        held_reason_now: Why it is held now.
        historical: Whether the item was computed from data that has since been replaced.
        data_status_now: The data status of the datasets the item reads, as they are now.
        flags_now: Flags that apply now, such as ``unverified_discrepancies`` or ``stale``.
        hidden_by: The active feedback hiding the item, if any.
    """

    held_now: bool
    held_reason_now: str | None = None
    historical: bool
    data_status_now: DataStatus
    flags_now: list[Flag]
    hidden_by: FeedbackKind | None = None


class DecisionSubjectRef(ApiModel):
    """The campaign or shared budget a "Record decision" on an item writes.

    Attributes:
        type: ``campaign`` or ``budget``.
        id: The subject's internal id in its dataset.
    """

    type: SubjectType
    id: int


class Item(ApiModel):
    """One ranked action card (AAP 0.5.3 ``Item``).

    Attributes:
        id: The ``recommendation`` id, used by ``POST /recommendations/{id}/feedback``.
        rank: Rank within its group, from 1.
        rule_id: The rule that produced it, for example ``K1`` or ``C0``.
        action: The recommended action as a headline.
        reason: The reason sentence, rendered from the evidence.
        entity: The entity the item is about.
        evidence: The frozen evidence.
        flags: Flags recorded at compute time.
        data_status: The data status at compute time.
        held: Whether the item was held at compute time.
        held_reason: Why it was held.
        decision_subject: The subject a "Record decision" on it writes, carried only by C1,
            C1b, C2 "Scale", B1, B2 and B3 items. ``None`` means no "Record decision": a C0
            discrepancy shows its resolver instead of feedback actions, and any other item
            offers "Mark done" in its place. On a historical item (``current.historical``)
            every action is disabled, the C0 resolver included.
        current: The read-time overlay.
    """

    id: int
    rank: int
    rule_id: str
    action: str
    reason: str
    entity: EntityView
    evidence: Evidence
    flags: list[Flag]
    data_status: DataStatus
    held: bool
    held_reason: str | None = None
    decision_subject: DecisionSubjectRef | None = None
    current: ItemOverlay


class Group(ApiModel):
    """A group of items within a section, for example ``block`` or ``discrepancies``.

    Attributes:
        key: The group key; creative groups are ``campaign:{id}`` and white-paper groups
            ``cohort:{currency}``.
        title: Display title.
        items: The items in rank order.
    """

    key: str
    title: str
    items: list[Item]


class Finding(ApiModel):
    """A non-action observation a rule emitted and the run stored (AAP 0.5.3 ``Finding``).

    Attributes:
        code: The finding code.
        section: The section the finding is shown in.
        entity: The entity it is about; ``None`` for a section- or account-wide finding.
        message: The rendered message.
        evidence: The numbers behind the message.
    """

    code: FindingCode
    section: SectionKey
    entity: EntityView | None = None
    message: str
    evidence: dict[str, Any]

    @classmethod
    def from_row(cls, row: RunFinding) -> Self:
        """Build the view of a stored ``run_finding`` row, exactly as recorded.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "code": row.code,
                "section": row.section,
                "entity": row.entity,
                "message": row.message,
                "evidence": row.evidence or {},
            }
        )


class WithheldSample(ApiModel):
    """The sample a withheld pattern saw against the minimum it needed.

    Both sizes are :data:`SampleSize`: integers for whole-number sizes, as ``evidence.sample``
    renders counts, and fixed-point decimal strings for fractional ones.

    Attributes:
        observed: The sample observed; ``None`` when no sample applies.
        required: The minimum required; ``None`` when no sample applies.
        unit: The sample's unit, for example ``clicks`` or ``days``.
    """

    observed: SampleSize
    required: SampleSize
    unit: str | None


class Withheld(ApiModel):
    """A pattern withheld for sample size or validity; it never becomes an item (P6).

    Attributes:
        rule_id: The rule whose pattern was withheld, for example ``B4``.
        entity: The entity it is about; ``None`` for an account-wide withholding.
        reason: The shortfall, for example "3 weekend days of 4 required".
        sample: Observed against required, in the rule's unit.
    """

    rule_id: str
    entity: EntityView | None
    reason: str
    sample: WithheldSample

    @classmethod
    def from_row(cls, row: RunWithheld) -> Self:
        """Build the view of a stored ``run_withheld`` row, exactly as recorded.

        The ``numeric`` sample sizes keep their stored values and render as :data:`SampleSize`:
        a whole-number size (``Decimal("1.000000")``) as the integer ``1``, as item evidence
        renders counts, and a fractional one as its fixed-point string.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "rule_id": row.rule_id,
                "entity": row.entity,
                "reason": row.reason,
                "sample": {"observed": row.observed, "required": row.required, "unit": row.unit},
            }
        )


class Section(ApiModel):
    """One of the five sections of the page.

    Attributes:
        key: The section key.
        title: Display title, for example "Keywords to adjust".
        groups: The section's groups, in display order.
        findings: The section's findings, shown above its items.
        withheld: Patterns withheld for sample size, with observed and required sizes.
        suppressed: Items hidden by feedback, blocked by G1 or suppressed by P2, by reason.
    """

    key: SectionKey
    title: str
    groups: list[Group]
    findings: list[Finding]
    withheld: list[Withheld]
    suppressed: dict[str, int]


class SourceModes(_PerSourceModes):
    """Each source's mode now, read live by the overlay (``Overlay.source_modes``).

    Attributes:
        google: Google's current mode; ``None`` before its first dataset.
        linkedin: LinkedIn's current mode.
        hubspot: HubSpot's current mode.
    """


class Overlay(ApiModel):
    """The page-level current state, computed at read time (AAP 0.3.2).

    Attributes:
        open_discrepancies: Every unresolved discrepancy now, including any detected after
            the run.
        source_modes: Each source's mode now.
        stale_datasets: Dataset names that are stale now.
        incomplete_datasets: Dataset names that are not complete over the run's window or an
            item's required range: a complete row whose coverage misses that range counts, a
            Q-G6 finest-partition shortfall only when the range includes one of its days.
        computed_at: When the overlay was computed.
    """

    open_discrepancies: int
    source_modes: SourceModes
    stale_datasets: list[str]
    incomplete_datasets: list[str]
    computed_at: AwareDatetime


class Banner(ApiModel):
    """A global banner, built from the run plus the overlay (AAP 0.5.5 fixed order).

    Attributes:
        code: The banner code, for example ``open_discrepancies`` or ``short_window``.
        tone: The design-system tone.
        message: The banner text.
        count: The count it reports, such as open discrepancies.
    """

    code: str
    tone: BannerTone
    message: str
    count: int | None = None


class RecommendationsPage(ApiModel):
    """``GET /recommendations``: the five sections of one run with the current-state overlay.

    Attributes:
        run: The run the sections come from.
        overlay: The page-level current state.
        banners: The global banners, in their fixed order.
        sections: Exactly five sections, in the order ``keywords``, ``campaigns``,
            ``budget``, ``creative``, ``white_papers``; an empty section is still present.
    """

    run: Run
    overlay: Overlay
    banners: list[Banner]
    sections: list[Section]

    @model_validator(mode="after")
    def _five_sections_in_order(self) -> Self:
        """Require the five sections in the fixed order (AAP 0.5.3)."""
        keys = tuple(section.key for section in self.sections)
        if keys != tuple(SECTION_ORDER):
            raise ValueError(
                f"sections must be exactly {list(SECTION_ORDER)} in that order, found {list(keys)}"
            )
        return self


class FeedbackRequest(RequestModel):
    """``POST /recommendations/{id}/feedback`` body.

    Attributes:
        kind: ``dismiss`` (30 days), ``snooze`` (7 days) or ``done`` (30 days), as configured.
        note: An optional note (:data:`StorableNote`).
    """

    kind: FeedbackKind
    note: StorableNote | None = None


class Feedback(ApiModel):
    """A recorded dismiss, snooze or done, suppressing its rule and entity until ``until``.

    Attributes:
        id: The feedback id.
        recommendation_id: The item it was given on.
        rule_id: The item's rule.
        entity_type: The item's entity type.
        entity_id: The item's stable entity key (``{source}:{type}:{external_id}``).
        kind: ``dismiss``, ``snooze`` or ``done``.
        until: When the suppression ends.
        note: The user's note.
        created_at: When the feedback was given.
        created_by: Who gave it.
    """

    id: int
    recommendation_id: int
    rule_id: str
    entity_type: str | None
    # recommendation_feedback.entity_id is a text key (EntityRef.key), not an integer id.
    entity_id: str | None
    kind: FeedbackKind
    until: AwareDatetime
    note: str | None
    created_at: AwareDatetime
    created_by: str | None

    @classmethod
    def from_row(cls, row: RecommendationFeedback) -> Self:
        """Build the view of a stored ``recommendation_feedback`` row.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "id": row.id,
                "recommendation_id": row.recommendation_id,
                "rule_id": row.rule_id,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "kind": row.kind,
                "until": row.until,
                "note": row.note,
                "created_at": row.created_at,
                "created_by": row.created_by,
            }
        )


# ---------------------------------------------------------------------------
# Decisions and discrepancies (AAP 0.5.2 patch model, 0.5.4.4)
# ---------------------------------------------------------------------------


class SubjectIn(RequestModel):
    """The decision subject a request names.

    Attributes:
        type: ``campaign`` or ``budget`` (a Google explicitly shared budget).
        id: The subject's internal id, 1 to :data:`BIGINT_MAX`.
    """

    type: SubjectType
    id: PositiveBigInt


def _publish_without_null(schema: JsonDict) -> None:
    """Publish an optional, never-``null`` request field as its non-null type alone.

    Pydantic renders ``X | None = None`` as ``{"anyOf": [X, {"type": "null"}], "default":
    null}``. The ``None`` only marks the field as absent and a client cannot send it, so the
    ``null`` branch and the ``null`` default are dropped; a single remaining branch is merged
    into the field schema, whose own keys (its ``title``) win.

    Args:
        schema: The field's generated JSON schema, updated in place.
    """
    schema.pop("default", None)
    branches = schema.get("anyOf")
    if not isinstance(branches, list):
        return
    kept = [branch for branch in branches if branch != {"type": "null"}]
    if len(kept) == 1 and isinstance(kept[0], dict):
        del schema["anyOf"]
        for key, value in kept[0].items():
            schema.setdefault(key, value)
    else:
        schema["anyOf"] = kept


class _ExpectedFieldsIn(RequestModel):
    """The three decidable fields, every one optional and never ``null``.

    A field is part of the submission only when it is present; an explicit ``null`` is
    refused, because a decided field cannot be cleared. Which fields a subject accepts, empty
    and no-op submissions, and the currency's minor unit are judged by
    ``ads_engine.decisions`` against the subject's decision shape, so those 422 rules live
    in one place.

    ``None`` stays in each annotation, so an absent field validates and a wrong value is
    reported at the field itself; :func:`_publish_without_null` keeps it out of the JSON
    schema, which therefore offers no ``null`` that :meth:`_no_explicit_null` would refuse.
    These are request bodies only, never serialised to a client.
    """

    status: CampaignStatus | None = Field(default=None, json_schema_extra=_publish_without_null)
    daily_budget: MoneyIn | None = Field(default=None, json_schema_extra=_publish_without_null)
    total_budget: MoneyIn | None = Field(default=None, json_schema_extra=_publish_without_null)

    @model_validator(mode="after")
    def _no_explicit_null(self) -> Self:
        """Refuse a field sent as ``null``: omit it instead."""
        nulls = [
            name
            for name in type(self).model_fields
            if name in self.model_fields_set and getattr(self, name) is None
        ]
        if nulls:
            raise ValueError(f"omit the field instead of sending null: {', '.join(nulls)}")
        return self

    def present_fields(self) -> dict[str, Any]:
        """Return only the fields the submission carried, in declaration order.

        Returns:
            Field name to value; amounts are ``Decimal`` and the status a string.
        """
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name in self.model_fields_set
        }


class DecisionPatchIn(_ExpectedFieldsIn):
    """The fields a decision submission decides; every other field keeps its decided value.

    The fields a subject accepts depend on its decision shape: ``own_budget`` takes status and
    its own budget fields, ``shared_member`` takes status only, ``budget`` takes exactly the
    one amount its period allows (AAP 0.5.2).

    An ``own_budget`` patch is completed with the decided amounts it does not send. When that
    result holds other budget fields than the campaign has now (its budget changed after the
    last decision), it answers 422 ``budget_changed``: resolve the budget discrepancy first,
    or, when the campaign only gained a budget field, send that field in the patch. A Google
    campaign whose budget period no entity sync reports is not checked and keeps its decided
    amount.

    Attributes:
        status: The decided campaign status.
        daily_budget: The decided daily budget, as a decimal string.
        total_budget: The decided lifetime or custom-period budget, as a decimal string.
    """


class DecisionRequest(RequestModel):
    """``POST /decisions`` body: a patch on one campaign or shared budget.

    Attributes:
        subject: The decision subject.
        patch: The fields being decided.
        recommendation_id: The item the decision was recorded from, if any; 1 to
            :data:`BIGINT_MAX`. An unknown item answers 404. The item must offer exactly
            this subject: one with no ``decision_subject`` answers 422
            ``decision_not_offered``, one naming another subject 422 ``subject_mismatch``.
            Every dataset its provenance names, and the subject's, must stay current until
            the decision commits, else 409 ``subject_retired``.
        note: An optional note (:data:`StorableNote`).
    """

    subject: SubjectIn
    patch: DecisionPatchIn
    recommendation_id: PositiveBigInt | None = None
    note: StorableNote | None = None


class IntendedStateIn(_ExpectedFieldsIn):
    """The intended state of a ``record_intended`` resolution, in the subject's shape.

    ``own_budget``: ``{status, daily_budget?, total_budget?}`` with the budget fields the
    campaign has; ``shared_member``: ``{status}``; ``budget``: ``{daily_budget}`` or
    ``{total_budget}`` per its period (AAP 0.5.4.4).

    Attributes:
        status: The intended campaign status.
        daily_budget: The intended daily budget, as a decimal string.
        total_budget: The intended lifetime or custom-period budget, as a decimal string.
    """


class ResolveRequest(RequestModel):
    """``POST /discrepancies/{id}/resolve`` body.

    ``platform_correct`` and ``adopt_platform`` adopt the snapshot the discrepancy compared
    (else the subject's newest), as does ``acknowledge_removal`` on a subject with no
    decision. They answer 409 ``platform_state_unknown`` while that state is unknown: no
    snapshot, a status outside the normalised set, a shared member without its budget, a
    budget amount or period Google did not report (of a shared budget, or of the budget an
    own-budget campaign's snapshot names), or a snapshot observed in another budget shape or
    period than the subject has now. ``platform_correct`` of a ``status`` discrepancy in an
    unchanged shape reads only the status, so an unreported amount does not refuse it. Retry
    after the next entity sync.

    Attributes:
        resolution: The resolution; which ones apply depends on the discrepancy's kind and
            the subject's decision shape (AAP 0.5.4.4).
        intended_state: Required by ``record_intended`` only.
        note: An optional note (:data:`StorableNote`).
    """

    resolution: ResolutionKey
    intended_state: IntendedStateIn | None = None
    note: StorableNote | None = None


class SubjectView(ApiModel):
    """A decision subject with what the log and resolver need to render it.

    Attributes:
        type: ``campaign`` or ``budget``.
        id: The subject's internal id.
        name: The campaign name, or the shared budget's display name.
        source: The subject's source.
        currency: The account currency its amounts are in.
    """

    type: SubjectType
    id: int
    name: str | None
    source: SourceName | None
    currency: str | None


def _optional_keys_schema(schema: dict[str, Any]) -> None:
    """Publish every key of a ``total=False`` view as optional in its JSON schema.

    Pydantic decides which keys of a typed dict are ``required`` under the configuration of
    the model that encloses it, so an enclosing model with
    ``json_schema_serialization_defaults_required`` would publish every optional key as
    required, and only where the view is inlined rather than shared through ``$defs``.
    Dropping ``required`` publishes what the view serialises: only the keys the stored object
    holds. It changes the JSON schema only, never validation or serialisation.
    """
    schema.pop("required", None)


_VIEW_DICT_CONFIG: Final = ConfigDict(extra="ignore", json_schema_extra=_optional_keys_schema)
"""Configuration of the stored-``jsonb`` views: unknown keys are ignored, as in
:class:`ApiModel`, and no key is published as required (:func:`_optional_keys_schema`)."""


@with_config(_VIEW_DICT_CONFIG)
class DecisionPatchView(TypedDict, total=False):
    """The ``patch`` of a :class:`Decision`: the fields the submission itself carried.

    Every key is optional and appears only when the stored ``decision.patch`` holds it; a key
    is never ``null``, because a decided field cannot be cleared. The stored object is
    returned key for key, and an unknown key is ignored (dropped), as :class:`ApiModel` does.
    The variants, by ``origin`` and the ``ads_engine.decisions`` writer:

    * ``user`` and ``recommendation`` rows (``record_decision``, from
      ``DecisionPatch.as_json``): exactly the fields the :class:`DecisionPatchIn` carried,
      for example ``{"daily_budget": "120.50"}``, amounts as fixed-point decimal strings.
    * ``baseline`` rows (``write_baseline``) and ``resolution`` rows
      (``resolve_discrepancy``, the ``acknowledge_removal`` tombstone included): ``{}``.

    Attributes:
        status: The decided campaign status.
        daily_budget: The decided daily budget.
        total_budget: The decided lifetime or custom-period budget.
    """

    status: CampaignStatus
    daily_budget: Money
    total_budget: Money


class Decision(ApiModel):
    """One decision log row: the complete materialised expected state for its shape.

    AAP 0.5.3 ("carrying ``shape`` and the complete materialised expected state"). A
    budget-only patch keeps the decided status, a status-only patch keeps the decided budget,
    and ``patch`` records what the submission itself carried.

    Attributes:
        id: The decision id.
        subject: The subject decided.
        shape: ``own_budget``, ``shared_member`` or ``budget``.
        status: The decided status; ``None`` for budget subjects.
        daily_budget: The decided daily budget; ``None`` where the shape has none.
        total_budget: The decided lifetime or custom-period budget.
        budget_id: For ``shared_member`` rows, the shared budget the campaign is assigned to.
        budget_external_id: For ``shared_member`` rows, the Google id of the shared budget the
            campaign is assigned to, the id the product names it by ("Shared budget
            {external_id}"); ``None`` for the other shapes, or when that budget row cannot be
            read.
        tombstone: Whether the row records an acknowledged removal.
        decided_at: When the decision was recorded.
        decided_by: Who recorded it; ``None`` for baseline and system rows.
        origin: ``baseline``, ``user``, ``recommendation`` or ``resolution``.
        recommendation_id: The item it was recorded from, if any.
        patch: The fields the submission carried (:class:`DecisionPatchView`); ``{}`` for
            baseline and resolution rows.
        note: The note entered with it.
    """

    id: int
    subject: SubjectView
    shape: DecisionShape
    status: CampaignStatus | None
    daily_budget: Money | None
    total_budget: Money | None
    budget_id: int | None
    budget_external_id: str | None
    tombstone: bool
    decided_at: AwareDatetime
    decided_by: str | None
    origin: DecisionOrigin
    recommendation_id: int | None
    patch: DecisionPatchView
    note: str | None

    @classmethod
    def from_row(
        cls,
        row: DecisionRow,
        *,
        subject: SubjectView,
        budget_external_id: str | None = None,
    ) -> Self:
        """Build the view of a stored ``decision`` row.

        Args:
            row: The decision row.
            subject: The subject view the route resolved for ``row.subject_id``.
            budget_external_id: The Google id of the shared budget ``row.budget_id`` assigns
                a ``shared_member`` row to, as the route read it within the decision's
                dataset; ``None`` (the default) when the row names no budget or that budget
                could not be read.

        Returns:
            The decision view.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "id": row.id,
                "subject": subject,
                "shape": row.shape,
                "status": row.status,
                "daily_budget": row.daily_budget,
                "total_budget": row.total_budget,
                "budget_id": row.budget_id,
                "budget_external_id": budget_external_id,
                "tombstone": row.tombstone,
                "decided_at": row.decided_at,
                "decided_by": row.decided_by,
                "origin": row.origin,
                "recommendation_id": row.recommendation_id,
                "patch": row.patch or {},
                "note": row.note,
            }
        )


@with_config(_VIEW_DICT_CONFIG)
class DiscrepancyValue(TypedDict, total=False):
    """One side of a :class:`Discrepancy`: the compared fields ``ads_engine.decisions`` stored.

    Every key is optional: a side holds only the keys its variant writes, and the response
    returns exactly the stored keys, a stored ``null`` as ``null``. An unknown key is ignored
    (dropped), as :class:`ApiModel` does. The variants, by writer:

    * **Status** (``compare``): ``{"status"}`` on both sides of a ``status`` discrepancy.
    * **Money** (``_money_value``; a ``budget`` discrepancy on an ``own_budget`` or ``budget``
      subject): ``{"daily_budget", "total_budget", "currency"}``, an amount ``null`` where
      the side has none, plus ``"amount_micros"`` on Google subjects only.
    * **Assignment** (``_assignment_value``; a ``budget`` discrepancy on a shared member, or
      on a campaign that moved onto a shared budget): ``{"budget_id", "budget_external_id"}``.
      ``budget_id`` is ``null`` where the side names the campaign's own budget (an
      own-budget decided state) or reports no budget; on the platform side it is the
      snapshot's budget id, shared or not. ``budget_external_id`` is set only for a shared
      budget the subject knows, so a ``budget_id`` without it, as a member moved onto an
      unshared budget reports, does not establish that the budget is shared.
    * **Tombstone reappearance** (``_compare_tombstone``): a campaign's decided side is
      ``{"status", "tombstone": true}`` and its platform side ``{"status"}``, ``null`` when
      unreported; a shared budget's sides are the money variant, the decided one with
      ``"tombstone": true``.
    * **Complete state** in the subject's shape (``describe_expected``: the decided side of a
      ``missing`` row and of one waiting after ``record_intended``; ``describe_snapshot``: the
      platform side of an ``untracked`` row): ``own_budget`` gives ``status`` plus the money
      keys, ``shared_member`` ``status`` plus the assignment keys, ``budget`` the money keys
      only; a tombstone decision adds ``"tombstone": true``, and a status or amount the
      snapshot does not report is ``null``. A waiting row refreshed without a snapshot merges
      the new platform keys into the stored ones (``_refresh_waiting``).

    Attributes:
        status: The normalised campaign status; ``None`` when the platform reported none.
        daily_budget: The daily budget; ``None`` where the side has none.
        total_budget: The lifetime or custom-period budget; ``None`` where the side has none.
        currency: The ISO 4217 code of the subject account's currency.
        amount_micros: Google only: the amount in micros, Google's native precision; ``None``
            when the side has no amount.
        budget_id: The internal id of the budget the campaign is assigned to on this side: on
            the platform side the snapshot's budget id, shared or not (against an own-budget
            decision, the subject's current shared budget when the snapshot names none); on
            the decided side the budget a ``shared_member`` decision assigns. ``None`` where
            the side names the campaign's own budget (an own-budget decided state) or reports
            no budget.
        budget_external_id: That budget's Google id, set only when it is a shared budget the
            subject knows (its current one or the one its decision assigns); ``None``
            otherwise. A ``budget_id`` with this ``None`` does not establish that the budget
            is shared.
        tombstone: Present, and ``True``, only on a side describing an ``acknowledge_removal``
            tombstone.
    """

    status: CampaignStatus | None
    daily_budget: Money | None
    total_budget: Money | None
    currency: str | None
    amount_micros: int | None
    budget_id: int | None
    budget_external_id: str | None
    tombstone: bool


class Discrepancy(ApiModel):
    """A mismatch between a subject's platform state and its last decision (rule C0).

    Attributes:
        id: The discrepancy id.
        subject: The subject.
        shape: The subject's current decision shape, which fixes the intended-state fields.
        kind: ``status``, ``budget``, ``untracked`` or ``missing``.
        decided_value: The decided side (:class:`DiscrepancyValue`). ``None`` for an
            ``untracked`` discrepancy until ``record_intended`` records an intended state
            and moves it to ``awaiting_platform``; it then shows that state, or a decision
            recorded since.
        platform_value: The platform side (:class:`DiscrepancyValue`); ``None`` for
            ``missing``.
        detected_at: When it was detected; the oldest are shown first.
        state: ``open``, ``awaiting_platform`` or ``resolved``.
        resolved_at: When it was resolved.
        resolution: The resolution applied, if any.
        resolutions: The resolutions valid for its (kind, shape) pair, in the fixed order of
            ``DISCREPANCY_RESOLUTIONS``; the resolver renders exactly these.
    """

    id: int
    subject: SubjectView
    shape: DecisionShape
    kind: DiscrepancyKind
    decided_value: DiscrepancyValue | None
    platform_value: DiscrepancyValue | None
    detected_at: AwareDatetime
    state: DiscrepancyState
    resolved_at: AwareDatetime | None
    resolution: ResolutionKey | None
    resolutions: list[ResolutionKey]

    @classmethod
    def from_row(
        cls,
        row: DiscrepancyRow,
        *,
        subject: SubjectView,
        shape: str,
        resolutions: Iterable[str],
    ) -> Self:
        """Build the view of a stored ``discrepancy`` row.

        Which resolutions are valid is decided by ``ads_engine.decisions.RESOLUTIONS``; the
        caller passes them in, and they are only put in their fixed display order here.

        Args:
            row: The discrepancy row.
            subject: The subject view the route resolved for ``row.subject_id``.
            shape: The subject's current decision shape.
            resolutions: The resolutions valid for the row's kind and the shape, in any order.

        Returns:
            The discrepancy view.

        Raises:
            ValueError: When ``resolutions`` holds a key outside ``DISCREPANCY_RESOLUTIONS``.
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        offered = set(resolutions)
        unknown = offered.difference(DISCREPANCY_RESOLUTIONS)
        if unknown:
            raise ValueError(f"unknown discrepancy resolutions {sorted(unknown)}")
        return cls.model_validate(
            {
                "id": row.id,
                "subject": subject,
                "shape": shape,
                "kind": row.kind,
                "decided_value": row.decided_value,
                "platform_value": row.platform_value,
                "detected_at": row.detected_at,
                "state": row.state,
                "resolved_at": row.resolved_at,
                "resolution": row.resolution,
                "resolutions": [key for key in DISCREPANCY_RESOLUTIONS if key in offered],
            }
        )


# ---------------------------------------------------------------------------
# Sources (AAP 0.5.3 ``GET /sources``, 0.5.5 ``/sources``)
# ---------------------------------------------------------------------------


class DatasetView(ApiModel):
    """One dataset of a source.

    Attributes:
        id: The ``source_dataset`` id.
        mode: ``live`` or ``simulated``.
        role: ``primary`` or ``companion``.
        state: ``building``, ``active`` or ``retired``.
        reason: Why the dataset exists, for example "credentials not configured".
        created_at: When it was created.
        activated_at: When it became active, if it has.
    """

    id: int
    mode: Mode
    role: DatasetRole
    state: DatasetState
    reason: str | None
    created_at: AwareDatetime
    activated_at: AwareDatetime | None

    @classmethod
    def from_row(cls, row: SourceDataset) -> Self:
        """Build the view of a ``source_dataset`` row.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "id": row.id,
                "mode": row.mode,
                "role": row.role,
                "state": row.state,
                "reason": row.reason,
                "created_at": row.created_at,
                "activated_at": row.activated_at,
            }
        )


class DatasetFreshnessView(ApiModel):
    """Freshness, coverage and completeness of one named dataset (AAP 0.3.2).

    Attributes:
        dataset: The dataset name, for example ``google.metrics_daily``.
        last_attempt_at: When a job last tried to refresh it.
        last_success_at: When a job last refreshed it successfully.
        covered_from: First native date covered.
        covered_through: Last native date covered.
        completeness: ``complete``, ``incomplete`` or ``unknown``.
        reason: Why it is not complete.
        stale: Whether it never succeeded (``last_success_at`` is ``None``) or last succeeded
            more than ``sources.stale_after_hours`` ago, decided by the route from the
            configuration.
    """

    dataset: str
    last_attempt_at: AwareDatetime | None
    last_success_at: AwareDatetime | None
    covered_from: date | None
    covered_through: date | None
    completeness: Completeness
    reason: str | None
    stale: bool

    # Derived, so it can never disagree with ``completeness``; pydantic's documented form for
    # a computed property needs this ignore under mypy.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def incomplete(self) -> bool:
        """Whether the dataset is not ``complete`` (``incomplete`` or ``unknown``)."""
        return self.completeness != "complete"

    @classmethod
    def from_row(cls, row: DatasetFreshness, *, stale: bool) -> Self:
        """Build the view of a ``dataset_freshness`` row.

        Args:
            row: The freshness row.
            stale: Whether the dataset is stale now, judged by the caller against
                ``sources.stale_after_hours``.

        Returns:
            The freshness view.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "dataset": row.dataset,
                "last_attempt_at": row.last_attempt_at,
                "last_success_at": row.last_success_at,
                "covered_from": row.covered_from,
                "covered_through": row.covered_through,
                "completeness": row.completeness,
                "reason": row.reason,
                "stale": stale,
            }
        )


class SyncRunView(ApiModel):
    """One recent sync job run.

    Attributes:
        id: The ``sync_run`` id.
        job: The job name.
        dataset: The dataset it refreshed, when a single one.
        started_at: When it started.
        finished_at: When it finished, if it has.
        status: ``running``, ``succeeded`` or ``failed``.
        rows: Rows written, once known.
        completeness: Completeness of what it read, once known.
        error: The redacted failure message of a failed run.
    """

    id: int
    job: str
    dataset: str | None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    status: SyncRunStatus
    rows: int | None
    completeness: Completeness | None
    error: str | None

    @classmethod
    def from_row(cls, row: SyncRun) -> Self:
        """Build the view of a ``sync_run`` row.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "id": row.id,
                "job": row.job,
                "dataset": row.dataset,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "status": row.status,
                "rows": row.rows,
                "completeness": row.completeness,
                "error": row.error,
            }
        )


class Source(ApiModel):
    """Health, mode, datasets, freshness and call budget of one source (``GET /sources``).

    ``calls_24h`` and ``call_cap`` describe the product's own cap on Google webhook calls
    (``GOOGLE_DAILY_CALL_BUDGET``, AAP 0.3.3), not Google's token-wide quota, and are ``None``
    for LinkedIn and HubSpot. The companion fields are set only for HubSpot while a simulated
    LinkedIn dataset's companion CRM dataset is active.

    Attributes:
        source: The source.
        mode: ``live`` or ``simulated``; ``None`` before the worker's first attempt and
            while no mode is established (live access never verified: a stored ``live``
            with no active dataset).
        status: ``ok``, ``stale``, ``denied`` or ``error``; ``None`` before the first attempt.
        reason: Why the source is in its mode or status.
        day_boundary: The IANA zone whose midnight ends the native dates of this source's
            freshness rows (``covered_from`` and ``covered_through``, primary and companion),
            so the UI can label them (AAP 0.5.4.1, 0.5.5): for Google the active Google
            dataset's account time zone (``customer.time_zone``), for LinkedIn and HubSpot
            ``UTC``. ``None`` while Google has no active dataset or no account row with a
            zone; the display time zone never stands in for it.
        active_dataset: The active primary dataset.
        companion_dataset: HubSpot only: the active companion CRM dataset.
        datasets: Freshness of the active dataset's named datasets.
        companion_datasets: Freshness of the companion's named datasets.
        last_attempt_at: When a sync job or a live probe last called the source.
        calls_24h: The product's own ``GOOGLE_DAILY_CALL_BUDGET`` usage: the Google webhook
            call reservations still counting in its sliding 24 h window. A call is booked
            once admitted, in the minute by which its POST has started (up to the 140 s
            dispatch bound ahead), and counts until 24 h after that minute ends; a call that
            had to be reserved again may count twice. So this is a conservative upper bound
            on the calls admitted in the trailing 24 h, not an exact count of dispatched
            POSTs.
        call_cap: The product's Google webhook call cap.
        sync_pending: Whether a "Sync now" request is pending.
        recent_runs: The newest sync runs, newest first.
    """

    source: SourceName
    mode: Mode | None
    status: ConnectionStatus | None
    reason: str | None
    day_boundary: str | None
    active_dataset: DatasetView | None
    companion_dataset: DatasetView | None
    datasets: list[DatasetFreshnessView]
    companion_datasets: list[DatasetFreshnessView]
    last_attempt_at: AwareDatetime | None
    calls_24h: int | None
    call_cap: int | None
    sync_pending: bool
    recent_runs: list[SyncRunView]


class SyncRequestView(ApiModel):
    """A queued "Sync now" request (``POST /sources/{source}/sync``, 202).

    Attributes:
        id: The ``sync_request`` id.
        source: The source to sync.
        requested_at: When it was requested.
        picked_at: When the worker picked it up; ``None`` while pending.
    """

    id: int
    source: SourceName
    requested_at: AwareDatetime
    picked_at: AwareDatetime | None = None

    @classmethod
    def from_row(cls, row: SyncRequest) -> Self:
        """Build the view of a ``sync_request`` row.

        Raises:
            pydantic.ValidationError: When the stored row holds a value outside the contract.
        """
        return cls.model_validate(
            {
                "id": row.id,
                "source": row.source,
                "requested_at": row.requested_at,
                "picked_at": row.picked_at,
            }
        )


# ---------------------------------------------------------------------------
# Config and health (AAP 0.5.3 ``GET /config``, ``GET /healthz``)
# ---------------------------------------------------------------------------


class WindowConfig(ApiModel):
    """The configured window settings; the UI reads window lengths only from here.

    Attributes:
        default_days: ``window.default_days``; the picker marks this preset "default".
        trusted_min_days: ``window.trusted_min_days``; shorter windows are flagged.
        presets: ``window.presets``, ascending: the picker's choices.
        max_days: ``window.max_days``: the longest custom window.
        sustained_min_days: ``window.sustained_min_days``.
    """

    default_days: int
    trusted_min_days: int
    presets: list[int]
    max_days: int
    sustained_min_days: int

    @classmethod
    def from_thresholds(cls, thresholds: Thresholds) -> Self:
        """Build the window settings from the loaded configuration, never from literals."""
        return cls(
            default_days=thresholds.default_days,
            trusted_min_days=thresholds.trusted_min_days,
            presets=list(thresholds.presets),
            max_days=thresholds.max_days,
            sustained_min_days=thresholds.sustained_min_days,
        )


class ContentAssetView(ApiModel):
    """One white paper of the registry (``config/content_assets.yaml``).

    Attributes:
        slug: The asset's identifier.
        title: Display title.
        lead_forms: Canonical lead-form URNs that resolve to the asset.
        creative_overrides: Creative URNs credited to the asset by override.
    """

    slug: str
    title: str
    lead_forms: list[str]
    creative_overrides: list[str]

    @classmethod
    def from_asset(cls, asset: ContentAsset) -> Self:
        """Build the view of a registry entry, in the file's own key names."""
        return cls(
            slug=asset.slug,
            title=asset.title,
            lead_forms=list(asset.lead_form_urns),
            creative_overrides=list(asset.creative_urns),
        )


class StageBindingView(ApiModel):
    """How one CRM count (MQL or Qualified Lead) is bound, as configured (AAP 0.5.4.7).

    The ``mql`` or ``ql`` entry of ``config/hubspot_mapping.yaml``, key for key as
    ``analysis.thresholds.StageBinding.to_json`` writes it. In ``stage_label`` mode the labels
    are resolved in order among the portal's ``lifecyclestage`` options and the first match
    wins; in ``property`` mode a contact date property records the event directly.

    Attributes:
        mode: ``stage_label`` or ``property``.
        stage_labels: The labels to try, in order; MQL's single ``stage_label`` is a
            one-element list.
        property: The bound contact property in ``property`` mode; ``None`` in
            ``stage_label`` mode.
    """

    mode: BindingMode
    stage_labels: list[str]
    property: str | None

    @classmethod
    def from_binding(cls, binding: StageBinding) -> Self:
        """Build the view of a configured binding, with ``StageBinding.to_json``'s values."""
        return cls(
            mode=binding.mode,
            stage_labels=list(binding.stage_labels),
            property=binding.property,
        )


class HubSpotMappingView(ApiModel):
    """``config/hubspot_mapping.yaml``: the MQL and QL bindings and the meeting rules.

    Key for key as ``analysis.thresholds.HubSpotMapping.to_json`` writes it (AAP 0.5.4.7).
    MQL and QL are bound independently and never blended.

    Attributes:
        mql: The MQL binding.
        ql: The Qualified Lead binding.
        booking_sources: ``hs_meeting_source`` values whose meetings can count as booked.
        meeting_outcome_exclusions: ``hs_meeting_outcome`` values never counted.
    """

    mql: StageBindingView
    ql: StageBindingView
    booking_sources: list[str]
    meeting_outcome_exclusions: list[str]

    @classmethod
    def from_mapping(cls, mapping: HubSpotMapping) -> Self:
        """Build the view of the loaded mapping, with ``HubSpotMapping.to_json``'s values."""
        return cls(
            mql=StageBindingView.from_binding(mapping.mql),
            ql=StageBindingView.from_binding(mapping.ql),
            booking_sources=list(mapping.booking_sources),
            meeting_outcome_exclusions=list(mapping.meeting_outcome_exclusions),
        )


class ResolvedBindingView(ApiModel):
    """One CRM count's binding as verified against the portal (AAP 0.3.3, 0.5.4.7).

    Key for key as ``analysis.thresholds.ResolvedBinding.to_json`` writes it: every key is
    present and the four text keys may be ``None``. An unavailable binding makes its count
    "unavailable", shown as such and never as 0.

    Attributes:
        available: Whether the count can be computed.
        property: The contact date property the count reads; set when available.
        stage_id: The lifecycle stage id the label resolved to, in ``stage_label`` mode.
        label: The label this binding resolved: the lifecycle stage label that matched in
            ``stage_label`` mode, or the configured display label kept in ``property`` mode,
            when one is set. Only the MQL binding's label heads the white-paper MQL column,
            e.g. "MQL (First meeting booked)".
        reason: Why the binding is unavailable; set when unavailable.
    """

    available: bool
    property: str | None
    stage_id: str | None
    label: str | None
    reason: str | None

    @classmethod
    def from_binding(cls, binding: ResolvedBinding) -> Self:
        """Build the view of a resolved binding, with ``ResolvedBinding.to_json``'s values."""
        return cls(
            available=binding.available,
            property=binding.property,
            stage_id=binding.stage_id,
            label=binding.label,
            reason=binding.reason,
        )


class GoogleCrmBindingView(ApiModel):
    """The optional Google-to-CRM link as verified against the portal (AAP 0.3.3).

    Key for key as ``analysis.thresholds.GoogleCrmBinding.to_json`` writes it: every key is
    present and the three text keys may be ``None``. Without the link, Google items carry
    ``no_crm_link`` and no Google QL count or pipeline veto is computed.

    Attributes:
        available: Whether the link can be used.
        campaign_property: The contact property holding the Google campaign id.
        touch_date_property: The datetime property holding when that touch happened.
        reason: Why the link is unavailable; set when unavailable.
    """

    available: bool
    campaign_property: str | None
    touch_date_property: str | None
    reason: str | None

    @classmethod
    def from_binding(cls, binding: GoogleCrmBinding) -> Self:
        """Build the view of the Google link, with ``GoogleCrmBinding.to_json``'s values."""
        return cls(
            available=binding.available,
            campaign_property=binding.campaign_property,
            touch_date_property=binding.touch_date_property,
            reason=binding.reason,
        )


class CrmBindingsView(ApiModel):
    """Every CRM binding verified against one HubSpot portal (``source_dataset.crm_bindings``).

    Key for key as ``analysis.thresholds.CrmBindings.to_json`` writes it (AAP 0.5.4.7).

    Attributes:
        mql: The resolved MQL binding.
        ql: The resolved Qualified Lead binding, independent of MQL.
        google: The Google-to-CRM link.
        verified_at: When the portal was checked, in UTC.
    """

    mql: ResolvedBindingView
    ql: ResolvedBindingView
    google: GoogleCrmBindingView
    verified_at: AwareDatetime

    @classmethod
    def from_bindings(cls, bindings: CrmBindings) -> Self:
        """Build the view of decoded bindings, with ``CrmBindings.to_json``'s values.

        ``verified_at`` is the same instant converted to UTC, as ``to_json`` stores it; it
        serialises as ISO 8601 with a ``Z`` offset like every other response instant.
        """
        return cls(
            mql=ResolvedBindingView.from_binding(bindings.mql),
            ql=ResolvedBindingView.from_binding(bindings.ql),
            google=GoogleCrmBindingView.from_binding(bindings.google),
            verified_at=bindings.verified_at.astimezone(UTC),
        )


class ConfigView(ApiModel):
    """``GET /config``: the read-only configuration behind ``/settings`` and the picker.

    Attributes:
        thresholds: Every threshold of ``config/thresholds.yaml``, by namespace.
        window: The window settings.
        hubspot_mapping: The MQL and Qualified Lead bindings and meeting rules as configured.
        crm_bindings: The bindings as resolved against HubSpot's active primary portal;
            ``None`` until verified.
        companion_crm_bindings: The bindings of the active companion CRM dataset; ``None``
            when there is none.
        content_assets: The white paper registry, in file order.
        config_hash: Hash of the three configuration files.
    """

    thresholds: dict[str, Any]
    window: WindowConfig
    hubspot_mapping: HubSpotMappingView
    crm_bindings: CrmBindingsView | None
    companion_crm_bindings: CrmBindingsView | None
    content_assets: list[ContentAssetView]
    config_hash: str


class Health(ApiModel):
    """``GET /healthz``: the process is up and the database answered.

    Attributes:
        status: Always ``ok``; a failure is reported as an error response instead.
    """

    status: Literal["ok"]
