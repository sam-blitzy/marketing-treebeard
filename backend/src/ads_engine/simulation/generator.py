"""The seeded simulated world behind every simulated source.

AAP 0.3.3 ("Contract fidelity", "Cross-source joins with simulation") and FR-11: when a source's
access is unavailable or denied, or ``DATA_MODE=simulated`` forces it, the product reads that
source from :class:`SimulatedWorld`, a deterministic world of Google Ads campaigns, budgets, ad
groups, keywords, search terms and conversion actions; LinkedIn accounts, campaign groups,
campaigns, creatives, posts, versioned lead forms, lead responses and demographic segments; and
the HubSpot contacts, lifecycle stages, meetings, deals and associations those leads become.

Design rules:

* **Wire-agnostic.** The world exposes typed Python records: snake_case attributes, ``Decimal``
  money, ``date`` native days and tz-aware UTC ``datetime`` instants. ``payloads.py`` renders
  them in each source's live wire shape against the contract files; no camelCase key, URN
  encoding, epoch-millisecond string or HubSpot string value is produced here.
* **Determinism (AAP NFR "simulated data is seeded").** Every random draw comes from a
  ``random.Random(f"{seed}:{namespace}:{key}")`` instance. String seeds are hashed with
  SHA-512, so draws are independent of ``PYTHONHASHSEED``. The global ``random`` module,
  ``hash()``, ``uuid4()``, set iteration order and the wall clock are never used for generation.
* **Lazy, pure daily facts.** A day's metrics are a pure function of ``(seed, entity key, native
  date)`` plus the spec, so any date range, backfill, restatement or 25,000-campaign scenario is
  cheap and reproducible, and changing one entity never shifts another entity's numbers. Count
  metrics use golden-ratio (Weyl) stochastic rounding: each day is an unbiased rounding of its
  expected value and any run of consecutive days sums to within about one unit of the expected
  total, so threshold behaviour holds for whatever window the moving clock selects.
* **Time** comes only from the injected ``clock`` and the spec's ``anchor``. Day offsets in
  specs (profiles, form versions, platform changes) are relative to the anchor; per-day noise
  is keyed by the absolute native date. Dates before the current native date are complete, the
  current native date is a partial day proportional to the elapsed time, and later dates do
  not exist yet. HubSpot records, lead responses and meetings appear only once their instants
  have passed.
* **Platform behaviour, not connector assumptions.** Google spends against daily budgets (a
  capped day loses at least 10% search impression share and spends about 95-102% of its
  budget: scaling aims the day at 97.5-101.5%, and rounding the scaled impressions and clicks
  to whole numbers can push it slightly past 102%),
  search-term spend never exceeds keyword spend, keyword spend never exceeds campaign spend,
  LinkedIn reports leads per creative and UTC day exactly as many as its non-test lead
  responses, campaigns include status ``COMPLETED`` (campaign groups do not), lead form edits
  create versions in which a question id can change meaning, and HubSpot associations are
  many-to-many with orphan deals.

Boundaries: imports only the standard library, ``ads_engine.simulation`` (reserved id ranges),
``ads_engine.settings``, ``ads_engine.logging``, ``ads_engine.analysis.thresholds`` and
``ads_engine.db.models.ops``. No database, network or file writes; the only file reads are the
config files through :meth:`Thresholds.load` when the caller passes no content assets or HubSpot
mapping. ``scenarios.py`` imports this module, never the reverse.

Example:
    >>> from datetime import UTC, date, datetime
    >>> clock = lambda: datetime(2026, 10, 2, 12, tzinfo=UTC)
    >>> world = SimulatedWorld(42, DEFAULT_SPEC.with_(anchor=date(2026, 10, 2)), clock=clock)
    ... # doctest: +SKIP
    >>> [form.id for form in world.linkedin_lead_forms(990000001)]  # doctest: +SKIP
    [990000101, 990000201, 990000301]
    >>> world.self_check()  # doctest: +SKIP
    ()
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import math
import random
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from functools import cached_property
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, Self, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ads_engine.analysis.thresholds import (
    CREATIVE_URN_PREFIX,
    FORM_URN_PREFIX,
    ContentAsset,
    HubSpotMapping,
    StageBinding,
    Thresholds,
)
from ads_engine.db.models.ops import SOURCES
from ads_engine.logging import get_logger
from ads_engine.settings import Settings, Source
from ads_engine.simulation import (
    GOOGLE_AD_GROUP_IDS,
    GOOGLE_BUDGET_IDS,
    GOOGLE_CAMPAIGN_IDS,
    GOOGLE_CONVERSION_ACTION_IDS,
    GOOGLE_CRITERION_IDS,
    HUBSPOT_CONTACT_IDS,
    HUBSPOT_DEAL_IDS,
    HUBSPOT_HASHED_CONTACT_IDS,
    HUBSPOT_MEETING_IDS,
    HUBSPOT_STAGE_IDS,
    LINKEDIN_ACCOUNT_IDS,
    LINKEDIN_CAMPAIGN_GROUP_IDS,
    LINKEDIN_CAMPAIGN_IDS,
    LINKEDIN_CREATIVE_IDS,
    LINKEDIN_FORM_IDS,
    LINKEDIN_POST_IDS,
    SIM_EMAIL_DOMAIN,
    SIM_GOOGLE_CUSTOMER_ID,
    IdRange,
    is_simulated_email,
)

__all__ = [
    "DEFAULT_HISTORY_DAYS",
    "DEFAULT_LIFECYCLE_STAGES",
    "DEFAULT_SPEC",
    "FAULT_KINDS",
    "FAULT_SURFACES",
    "SEGMENT_PIVOTS",
    "BulkDealSpec",
    "ContactRecord",
    "DailyProfile",
    "DayMetrics",
    "DealRecord",
    "ExplicitDays",
    "Fault",
    "FaultKind",
    "FormVersionSpec",
    "FunnelSpec",
    "GoogleAdGroupRecord",
    "GoogleAdGroupSpec",
    "GoogleBudgetRecord",
    "GoogleBudgetSpec",
    "GoogleBulkSpec",
    "GoogleCampaignDay",
    "GoogleCampaignRecord",
    "GoogleCampaignSpec",
    "GoogleConversionActionRecord",
    "GoogleConversionActionSpec",
    "GoogleCustomerRecord",
    "GoogleKeywordDay",
    "GoogleKeywordRecord",
    "GoogleKeywordSpec",
    "GoogleSearchTermDay",
    "GoogleSearchTermSpec",
    "GoogleSpec",
    "HubSpotSpec",
    "LinkedInAccountRecord",
    "LinkedInAccountSpec",
    "LinkedInCampaignGroupRecord",
    "LinkedInCampaignGroupSpec",
    "LinkedInCampaignRecord",
    "LinkedInCampaignSpec",
    "LinkedInCreativeDay",
    "LinkedInCreativeRecord",
    "LinkedInCreativeSpec",
    "LinkedInFormVersionRecord",
    "LinkedInLeadAnswer",
    "LinkedInLeadFormRecord",
    "LinkedInLeadFormSpec",
    "LinkedInLeadResponse",
    "LinkedInPostRecord",
    "LinkedInQuestionRecord",
    "LinkedInSegmentRecord",
    "LinkedInSegmentSpec",
    "LinkedInSpec",
    "MeetingRecord",
    "PlatformChange",
    "Profile",
    "PropertyOption",
    "PropertyRecord",
    "PropertyValue",
    "QualityScoreProfile",
    "QuestionSpec",
    "ScenarioLike",
    "SearchFilter",
    "SegmentValueSpec",
    "SimulatedWorld",
    "StageSpec",
    "WorldSpec",
    "WorldState",
]

_log = get_logger("ads_engine.simulation")

# ---------------------------------------------------------------------------
# Constants and value types
# ---------------------------------------------------------------------------

DEFAULT_HISTORY_DAYS: Final = 210
"""Default history length: the 180-day backfill (``BACKFILL_DAYS``) plus restatement windows."""

type GoogleBudgetPeriod = Literal["DAILY", "CUSTOM_PERIOD"]
type GoogleCampaignStatus = Literal["ENABLED", "PAUSED", "REMOVED"]
type GoogleCriterionStatus = Literal["ENABLED", "PAUSED", "REMOVED"]
type GoogleSearchTermStatus = Literal["NONE", "ADDED", "EXCLUDED", "ADDED_EXCLUDED"]
type GoogleMatchType = Literal["EXACT", "PHRASE", "BROAD"]
type GoogleCountingType = Literal["ONE_PER_CLICK", "MANY_PER_CLICK"]
type GoogleConversionActionStatus = Literal["ENABLED", "HIDDEN", "REMOVED"]
type LinkedInCampaignStatus = Literal[
    "ACTIVE", "PAUSED", "ARCHIVED", "COMPLETED", "CANCELED", "DRAFT", "PENDING_DELETION", "REMOVED"
]
type LinkedInCampaignGroupStatus = Literal[
    "ACTIVE", "PAUSED", "ARCHIVED", "CANCELED", "DRAFT", "PENDING_DELETION", "REMOVED"
]
type LinkedInCreativeStatus = Literal[
    "ACTIVE", "PAUSED", "DRAFT", "ARCHIVED", "CANCELED", "PENDING_DELETION", "REMOVED"
]
type LinkedInCtaLabel = Literal[
    "APPLY",
    "DOWNLOAD",
    "VIEW_QUOTE",
    "LEARN_MORE",
    "SIGN_UP",
    "SUBSCRIBE",
    "REGISTER",
    "REQUEST_DEMO",
    "JOIN",
    "ATTEND",
    "UNLOCK_FULL_DOCUMENT",
]
type SegmentPivot = Literal["MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY"]
type HubSpotObjectType = Literal["contacts", "meetings", "deals"]
type PropertyValue = str | Decimal | datetime | date | None
"""A typed HubSpot property value; ``payloads.py`` renders it as HubSpot's string or null."""

_GOOGLE_CAMPAIGN_STATUSES: Final = ("ENABLED", "PAUSED", "REMOVED")
_GOOGLE_SEARCH_TERM_STATUSES: Final = ("NONE", "ADDED", "EXCLUDED", "ADDED_EXCLUDED")
_GOOGLE_MATCH_TYPES: Final = ("EXACT", "PHRASE", "BROAD")
_GOOGLE_COUNTING_TYPES: Final = ("ONE_PER_CLICK", "MANY_PER_CLICK")
_GOOGLE_CONVERSION_ACTION_STATUSES: Final = ("ENABLED", "HIDDEN", "REMOVED")
# AttributionModelEnum values a conversion action can carry in v25: new actions take only
# last click or data-driven; EXTERNAL marks imported credit; retired rule-based models were
# upgraded to data-driven (AAP 0.2.4).
_GOOGLE_ATTRIBUTION_MODELS: Final = (
    "GOOGLE_ADS_LAST_CLICK",
    "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
    "EXTERNAL",
)
_GOOGLE_CHANNELS: Final = (
    "SEARCH",
    "DISPLAY",
    "SHOPPING",
    "VIDEO",
    "MULTI_CHANNEL",
    "PERFORMANCE_MAX",
    "DEMAND_GEN",
)
_LINKEDIN_CAMPAIGN_STATUSES: Final = (
    "ACTIVE",
    "PAUSED",
    "ARCHIVED",
    "COMPLETED",
    "CANCELED",
    "DRAFT",
    "PENDING_DELETION",
    "REMOVED",
)
_LINKEDIN_GROUP_STATUSES: Final = (
    "ACTIVE",
    "PAUSED",
    "ARCHIVED",
    "CANCELED",
    "DRAFT",
    "PENDING_DELETION",
    "REMOVED",
)
_LINKEDIN_CREATIVE_STATUSES: Final = (
    "ACTIVE",
    "PAUSED",
    "DRAFT",
    "ARCHIVED",
    "CANCELED",
    "PENDING_DELETION",
    "REMOVED",
)
_LINKEDIN_CTA_LABELS: Final = (
    "APPLY",
    "DOWNLOAD",
    "VIEW_QUOTE",
    "LEARN_MORE",
    "SIGN_UP",
    "SUBSCRIBE",
    "REGISTER",
    "REQUEST_DEMO",
    "JOIN",
    "ATTEND",
    "UNLOCK_FULL_DOCUMENT",
)
_LINKEDIN_OBJECTIVES: Final = (
    "LEAD_GENERATION",
    "WEBSITE_CONVERSION",
    "WEBSITE_VISIT",
    "BRAND_AWARENESS",
    "ENGAGEMENT",
    "VIDEO_VIEW",
    "JOB_APPLICANT",
    "TALENT_LEAD",
)

SEGMENT_PIVOTS: Final[tuple[SegmentPivot, ...]] = (
    "MEMBER_JOB_FUNCTION",
    "MEMBER_SENIORITY",
    "MEMBER_INDUSTRY",
)
"""The demographic pivots LinkedIn segment statistics are fetched for (AAP 0.3.3)."""

_SEGMENT_VALUE_PREFIXES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "MEMBER_JOB_FUNCTION": "urn:li:function:",
        "MEMBER_SENIORITY": "urn:li:seniority:",
        "MEMBER_INDUSTRY": "urn:li:industry:",
    }
)

# The demographic values every delivering campaign reaches, with their share of impressions,
# when a spec gives no explicit distribution for the (campaign, pivot) pair. Shares sum to 1.
_DEFAULT_SEGMENT_SHARES: Final[Mapping[str, tuple[tuple[str, str], ...]]] = MappingProxyType(
    {
        "MEMBER_JOB_FUNCTION": (
            ("urn:li:function:15", "0.34"),  # Marketing
            ("urn:li:function:25", "0.20"),  # Sales
            ("urn:li:function:4", "0.12"),  # Business Development
            ("urn:li:function:13", "0.11"),  # Information Technology
            ("urn:li:function:10", "0.08"),  # Finance
            ("urn:li:function:8", "0.08"),  # Engineering
            ("urn:li:function:18", "0.07"),  # Operations
        ),
        "MEMBER_SENIORITY": (
            ("urn:li:seniority:3", "0.18"),  # Entry
            ("urn:li:seniority:4", "0.30"),  # Senior
            ("urn:li:seniority:5", "0.22"),  # Manager
            ("urn:li:seniority:6", "0.12"),  # Director
            ("urn:li:seniority:7", "0.08"),  # VP
            ("urn:li:seniority:8", "0.06"),  # CXO
            ("urn:li:seniority:10", "0.04"),  # Owner
        ),
        "MEMBER_INDUSTRY": (
            ("urn:li:industry:4", "0.30"),  # Software Development
            ("urn:li:industry:96", "0.20"),  # IT Services and IT Consulting
            ("urn:li:industry:80", "0.15"),  # Advertising Services
            ("urn:li:industry:6", "0.13"),  # Technology, Information and Internet
            ("urn:li:industry:43", "0.12"),  # Financial Services
            ("urn:li:industry:1810", "0.10"),  # Professional Services
        ),
    }
)

FAULT_SURFACES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "google": (
            "search",
            "row_customer",
            "row_campaign",
            "row_campaign_daily",
            "row_ad_group",
            "row_keyword_view",
            "row_search_term_view",
            "row_ad_group_criterion",
            "row_conversion_action",
        ),
        "linkedin": (
            "access_token",
            "ad_account",
            "ad_campaign_groups",
            "ad_campaigns",
            "creatives",
            "posts",
            "ad_analytics",
            "ad_analytics_statistics",
            "lead_forms",
            "lead_form_responses",
            "lead_form_responses_batch",
        ),
        "hubspot": (
            "objects_batch_read",
            "crm_search",
            "associations_batch_read",
            "properties",
        ),
    }
)
"""The contract surface names a :class:`Fault` can target, per source.

These are the names of the contract files under ``tests/contracts/{source}/`` that
``payloads.py`` identifies requests by (its ``SURFACES``). A fault names the surface whose
request it disturbs; Google faults use ``search``, the one webhook surface.
"""

type FaultKind = Literal[
    "http_status",
    "timeout",
    "body_error",
    "batch_errors",
    "omit_ids",
    "continuation_failure",
    "non_complete_status",
    "decoration_mismatch",
    "no_decoration",
]

FAULT_KINDS: Final[tuple[FaultKind, ...]] = (
    "http_status",
    "timeout",
    "body_error",
    "batch_errors",
    "omit_ids",
    "continuation_failure",
    "non_complete_status",
    "decoration_mismatch",
    "no_decoration",
)
"""Every way a :class:`Fault` can make a simulated platform misbehave."""

# HubSpot default lifecycle stages: (internal value, label), in pipeline order.
DEFAULT_LIFECYCLE_STAGES: Final[tuple[tuple[str, str], ...]] = (
    ("subscriber", "Subscriber"),
    ("lead", "Lead"),
    ("marketingqualifiedlead", "Marketing Qualified Lead"),
    ("salesqualifiedlead", "Sales Qualified Lead"),
    ("opportunity", "Opportunity"),
    ("customer", "Customer"),
    ("evangelist", "Evangelist"),
    ("other", "Other"),
)
"""HubSpot's default ``lifecyclestage`` options as ``(value, label)``, in pipeline order."""

_MEETING_OUTCOMES: Final = ("SCHEDULED", "COMPLETED", "RESCHEDULED", "NO_SHOW", "CANCELED")
_MEETING_SOURCES: Final = (
    "MEETINGS_PUBLIC",
    "CRM_UI",
    "INTEGRATION",
    "BIDIRECTIONAL_SYNC",
    "BCC_TO_CRM",
    "EMAIL_INTEGRATION",
)
_DEAL_STAGES: Final = (
    "appointmentscheduled",
    "qualifiedtobuy",
    "presentationscheduled",
    "decisionmakerboughtin",
    "contractsent",
    "closedwon",
    "closedlost",
)
_SEARCH_OPERATORS: Final = (
    "EQ",
    "NEQ",
    "LT",
    "LTE",
    "GT",
    "GTE",
    "BETWEEN",
    "IN",
    "HAS_PROPERTY",
    "NOT_HAS_PROPERTY",
)

_CENT: Final = Decimal("0.01")
_MICRO: Final = Decimal("0.000001")
_SHARE_PLACES: Final = Decimal("0.0001")
_MICROS_PER_UNIT: Final = 1_000_000
# Golden-ratio conjugate: the step of the low-discrepancy sequence used for count rounding.
_GOLDEN: Final = 0.6180339887498949
_DAY: Final = timedelta(days=1)

# Offsets inside the HubSpot object-id ranges (each holds 10**11 ids), so each family of
# simulated objects has its own block and ids never collide: persons' objects from 0,
# Google-touch contacts' objects from 10**10, orphan deals from 2 * 10**10, bulk deals from
# 3 * 10**10, and objects of seeded contacts answering real lead emails from 5 * 10**10 (two
# per contact, see _CrmModel._hashed_object). Seeded contacts take 2 * 10**10 ids, so their
# objects end below 9 * 10**10.
_GOOGLE_CRM_BLOCK: Final = 10**10
_ORPHAN_DEAL_BLOCK: Final = 2 * 10**10
_BULK_DEAL_BLOCK: Final = 3 * 10**10
_HASHED_OBJECT_BLOCK: Final = 5 * 10**10
_HASHED_CONTACT_SPAN: Final = 2 * 10**10
_HASHED_OBJECTS_PER_CONTACT: Final = 2

_FIRST_NAMES: Final = (
    "Avery",
    "Blake",
    "Casey",
    "Dana",
    "Emery",
    "Finley",
    "Harper",
    "Jordan",
    "Kendall",
    "Logan",
    "Morgan",
    "Parker",
    "Quinn",
    "Reese",
    "Riley",
    "Rowan",
    "Sawyer",
    "Skyler",
    "Taylor",
    "Wren",
)
_LAST_NAMES: Final = (
    "Abbott",
    "Bennett",
    "Carver",
    "Dalton",
    "Ellison",
    "Foster",
    "Garner",
    "Hayes",
    "Ingram",
    "Jensen",
    "Keller",
    "Lowell",
    "Mercer",
    "Nolan",
    "Porter",
    "Ramsey",
    "Sutton",
    "Thayer",
    "Vance",
    "Whitaker",
)
_COMPANIES: Final = (
    "Northwind Analytics",
    "Blue Harbor Software",
    "Copperline Systems",
    "Evergreen Data",
    "Granite Peak Labs",
    "Lumen Ridge",
    "Maple & Finch",
    "Orbital Metrics",
    "Quarry Street Media",
    "Silverbirch Group",
)
_JOB_TITLES: Final = (
    "Marketing Manager",
    "Head of Demand Generation",
    "VP Marketing",
    "Growth Lead",
    "Marketing Operations Manager",
    "Director of Revenue Operations",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class _Replaceable:
    """Mixin giving the frozen spec dataclasses a ``with_(**changes)`` copy helper."""

    __slots__ = ()

    def with_(self, **changes: Any) -> Self:
        """Return a copy with ``changes`` applied, validated like a new instance.

        A thin wrapper on :func:`dataclasses.replace`, so a scenario derives a twin by moving a
        single input: ``profile.with_(conversion_rate=Decimal("0"))``.

        Raises:
            TypeError: When a name is not a field of this spec.
            ValueError: When the changed spec fails validation.
        """
        return cast(Self, dataclasses.replace(cast(Any, self), **changes))


def _require(condition: bool, message: str) -> None:
    """Raise ``ValueError(message)`` unless ``condition`` holds."""
    if not condition:
        raise ValueError(message)


def _frozen[K, V](mapping: Mapping[K, V]) -> Mapping[K, V]:
    """Return a read-only copy of ``mapping`` that keeps its insertion order."""
    return MappingProxyType(dict(mapping))


def _decimal_places(value: Decimal) -> int:
    """Return the number of decimal places ``value`` carries (0 for integers)."""
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _check_decimal(
    name: str,
    value: object,
    *,
    minimum: Decimal | None = Decimal(0),
    maximum: Decimal | None = None,
    places: int = 6,
) -> None:
    """Validate a ``Decimal`` spec value: finite, bounded and with at most ``places`` decimals.

    Raises:
        ValueError: Naming the field and the violated rule.
    """
    _require(isinstance(value, Decimal), f"{name} must be a Decimal, found {type(value).__name__}")
    number = cast(Decimal, value)
    _require(number.is_finite(), f"{name} must be finite, found {number}")
    if minimum is not None:
        _require(number >= minimum, f"{name} must be >= {minimum}, found {number}")
    if maximum is not None:
        _require(number <= maximum, f"{name} must be <= {maximum}, found {number}")
    _require(
        _decimal_places(number) <= places,
        f"{name} must have at most {places} decimal places, found {number}",
    )


def _check_int(name: str, value: object, *, minimum: int | None = 0) -> None:
    """Validate an integer spec value (``bool`` is rejected).

    Raises:
        ValueError: Naming the field and the violated rule.
    """
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{name} must be an integer, found {value!r}",
    )
    if minimum is not None:
        _require(cast(int, value) >= minimum, f"{name} must be >= {minimum}, found {value}")


def _check_choice(name: str, value: object, choices: Sequence[str]) -> None:
    """Validate that ``value`` is one of ``choices``.

    Raises:
        ValueError: Listing the allowed values.
    """
    _require(value in choices, f"{name} must be one of {', '.join(choices)}, found {value!r}")


def _check_text(name: str, value: object) -> None:
    """Validate a non-blank string.

    Raises:
        ValueError: When ``value`` is not a string or is blank.
    """
    _require(isinstance(value, str) and bool(value.strip()), f"{name} must be a non-blank string")


def _check_unique(what: str, keys: Iterable[object]) -> None:
    """Validate that ``keys`` holds no repeats.

    Raises:
        ValueError: Naming the first repeated key.
    """
    seen: set[object] = set()
    for key in keys:
        _require(key not in seen, f"{what} {key!r} is defined twice")
        seen.add(key)


def _rng(seed: int, namespace: str, key: str) -> random.Random:
    """Return the deterministic generator of one ``(seed, namespace, key)``.

    ``random.Random`` hashes a string seed with SHA-512, so the stream does not depend on
    ``PYTHONHASHSEED`` or the process.
    """
    return random.Random(f"{seed}:{namespace}:{key}")


def _count(expected: float, threshold: float) -> int:
    """Round an expected count with a threshold in ``[0, 1)``: ``floor(expected + threshold)``.

    With a uniformly distributed threshold this is unbiased stochastic rounding; with the
    golden-ratio sequence of :meth:`_Core.weyl` the sum over consecutive days stays within about
    one unit of the summed expectation.
    """
    if expected <= 0.0 or not math.isfinite(expected):
        return 0
    return math.floor(expected + threshold)


def _money(value: float | Decimal) -> Decimal:
    """Quantize an amount to cents with banker's rounding."""
    return Decimal(value).quantize(_CENT, ROUND_HALF_EVEN)


def _share(value: float) -> Decimal:
    """Quantize a share in ``[0, 1]`` to four decimals."""
    return Decimal(min(max(value, 0.0), 1.0)).quantize(_SHARE_PLACES, ROUND_HALF_EVEN)


def _to_micros(amount: Decimal) -> int:
    """Convert an amount to integer micros exactly.

    Raises:
        ValueError: When the amount is finer than one micro.
    """
    scaled = amount * _MICROS_PER_UNIT
    _require(scaled == scaled.to_integral_value(), f"amount {amount} is finer than one micro")
    return int(scaled)


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of ``text`` (UTF-8)."""
    return hashlib.sha256(text.encode()).hexdigest()


def _normalise_email(email: str) -> str:
    """Normalise an email the way lead ingestion does before hashing: trimmed and lowercased."""
    return email.strip().lower()


def _midnight(day: date, tz: ZoneInfo | None = None) -> datetime:
    """Return the instant (UTC) at which ``day`` starts in ``tz`` (UTC when ``None``)."""
    if tz is None:
        return datetime.combine(day, time(0), tzinfo=UTC)
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


def _epoch_ms(instant: datetime) -> int:
    """Return an aware instant as epoch milliseconds."""
    return int((instant - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1))


def _require_aware(name: str, instant: datetime) -> datetime:
    """Return ``instant`` in UTC.

    Raises:
        ValueError: When the datetime is naive.
    """
    _require(
        instant.tzinfo is not None and instant.utcoffset() is not None,
        f"{name} must be timezone-aware",
    )
    return instant.astimezone(UTC)


def _plain_id(value: int | str, prefix: str, what: str) -> int:
    """Return the numeric id of ``value``, a plain id or a ``{prefix}{id}`` URN.

    Raises:
        ValueError: When the value is neither.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.removeprefix(prefix)
        if text.isascii() and text.isdecimal():
            return int(text)
    raise ValueError(f"{what} must be an id or a {prefix}{{id}} URN, found {value!r}")


# ---------------------------------------------------------------------------
# Metric profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayMetrics(_Replaceable):
    """One entity's metrics on one native day, as a spec states them or the world computes them.

    Scenarios use it inside :class:`ExplicitDays` for exact threshold cases (``n*`` clicks,
    3.0 versus 2.9 expected outcomes, 5 of 7 capped days). Additive metrics default to zero;
    ``None`` means unavailable, never zero.

    Attributes:
        cost: Spend in the account currency.
        impressions: Impressions.
        clicks: Clicks.
        conversions: Google conversions; fractional under data-driven attribution, at most six
            decimals.
        conversions_value: Google conversion value in the account currency.
        form_opens: LinkedIn lead form opens.
        leads: LinkedIn native leads; equals the day's non-test lead responses.
        landing_page_clicks: LinkedIn landing-page clicks.
        website_conversions: LinkedIn external website conversions.
        budget_lost_is: Google search impression share lost to budget, in ``[0, 1]``.
        impression_share: Google search impression share, in ``[0, 1]``.
        historical_quality_score: Google keyword historical quality score (1-10).
        average_cpc: Google average cost per click; derived from cost and clicks when ``None``.
    """

    cost: Decimal = Decimal(0)
    impressions: int = 0
    clicks: int = 0
    conversions: Decimal = Decimal(0)
    conversions_value: Decimal = Decimal(0)
    form_opens: int = 0
    leads: int = 0
    landing_page_clicks: int = 0
    website_conversions: int = 0
    budget_lost_is: Decimal | None = None
    impression_share: Decimal | None = None
    historical_quality_score: int | None = None
    average_cpc: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate ranges: non-negative metrics, shares in [0, 1], quality score 1-10."""
        _check_decimal("cost", self.cost)
        _check_decimal("conversions", self.conversions)
        _check_decimal("conversions_value", self.conversions_value)
        for name in (
            "impressions",
            "clicks",
            "form_opens",
            "leads",
            "landing_page_clicks",
            "website_conversions",
        ):
            _check_int(name, getattr(self, name))
        _require(self.clicks <= self.impressions, "clicks cannot exceed impressions")
        _require(self.leads <= self.form_opens, "leads cannot exceed form opens")
        for name in ("budget_lost_is", "impression_share"):
            value = getattr(self, name)
            if value is not None:
                _check_decimal(name, value, maximum=Decimal(1))
        if self.historical_quality_score is not None:
            _check_int("historical_quality_score", self.historical_quality_score, minimum=1)
            _require(self.historical_quality_score <= 10, "historical_quality_score must be <= 10")
        if self.average_cpc is not None:
            _check_decimal("average_cpc", self.average_cpc)

    @property
    def is_zero(self) -> bool:
        """Whether the day has no delivery at all (no impressions, clicks or spend)."""
        return self.impressions == 0 and self.clicks == 0 and self.cost == 0


_ZERO_DAY: Final = DayMetrics()


def _plus(left: DayMetrics, right: DayMetrics) -> DayMetrics:
    """Add two days' additive metrics; the non-additive ones are left unavailable."""
    return DayMetrics(
        cost=left.cost + right.cost,
        impressions=left.impressions + right.impressions,
        clicks=left.clicks + right.clicks,
        conversions=left.conversions + right.conversions,
        conversions_value=left.conversions_value + right.conversions_value,
        form_opens=left.form_opens + right.form_opens,
        leads=left.leads + right.leads,
        landing_page_clicks=left.landing_page_clicks + right.landing_page_clicks,
        website_conversions=left.website_conversions + right.website_conversions,
    )


@dataclass(frozen=True, slots=True)
class DailyProfile(_Replaceable):
    """Seeded generator parameters for one entity's daily delivery.

    Each day draws a small multiplicative jitter (``noise``) on impressions and CPC from the
    day's own generator; counts are derived through golden-ratio rounding, so sustained
    patterns hold over any window. Outcome rates are per click (Google conversions, LinkedIn
    website conversions) or per form open (LinkedIn leads).

    Attributes:
        impressions_mean: Mean impressions per serving day.
        ctr: Clicks per impression.
        cpc: Cost per click in the account currency, before ``trend``.
        conversion_rate: Google conversions, or LinkedIn website conversions, per click. May
            exceed 1 (``MANY_PER_CLICK`` counting).
        spend_day_share: Share of days with any delivery; 1 means every day.
        weekend_factor: Multiplier on the outcome rate on Saturdays and Sundays (native day).
        fractional_conversions: Google conversions carry data-driven style fractional values
            with at most six decimals.
        form_open_rate: LinkedIn lead form opens per impression (lead-gen creatives); at most
            ``ctr``, because form opens are clicks.
        form_completion_rate: LinkedIn leads per form open.
        trend: Relative CPC drift per day (0.02 = +2% of the base CPC per day), from
            ``trend_from_offset``; the factor never falls below 5%.
        trend_from_offset: Day offset (relative to the anchor) where the drift starts; the
            first history day when ``None``.
        active_from_offset: First day offset with delivery; ``None`` = from history start.
        active_until_offset: Last day offset with delivery; ``None`` = open-ended. A spec
            entity whose status is not serving but whose profile ends here served until then.
        noise: Relative jitter on impressions (and half of it on CPC), in ``[0, 0.9]``.
        value_per_conversion: Google conversion value per conversion.
        landing_page_click_share: LinkedIn landing-page clicks per click (non lead-gen).
    """

    impressions_mean: int
    ctr: Decimal
    cpc: Decimal
    conversion_rate: Decimal = Decimal(0)
    spend_day_share: Decimal = Decimal(1)
    weekend_factor: Decimal = Decimal(1)
    fractional_conversions: bool = False
    form_open_rate: Decimal = Decimal(0)
    form_completion_rate: Decimal = Decimal(0)
    trend: Decimal = Decimal(0)
    trend_from_offset: int | None = None
    active_from_offset: int | None = None
    active_until_offset: int | None = None
    noise: Decimal = Decimal("0.1")
    value_per_conversion: Decimal = Decimal(100)
    landing_page_click_share: Decimal = Decimal("0.9")

    def __post_init__(self) -> None:
        """Validate every rate and window.

        Raises:
            ValueError: Naming the first invalid parameter.
        """
        _check_int("impressions_mean", self.impressions_mean)
        _check_decimal("ctr", self.ctr, maximum=Decimal(1), places=8)
        _check_decimal("cpc", self.cpc)
        _check_decimal("conversion_rate", self.conversion_rate, maximum=Decimal(10), places=8)
        _check_decimal("spend_day_share", self.spend_day_share, maximum=Decimal(1), places=8)
        _check_decimal("weekend_factor", self.weekend_factor, maximum=Decimal(10), places=8)
        _check_decimal("form_open_rate", self.form_open_rate, maximum=Decimal(1), places=8)
        _check_decimal(
            "form_completion_rate", self.form_completion_rate, maximum=Decimal(1), places=8
        )
        _require(
            self.form_open_rate <= self.ctr,
            "form_open_rate cannot exceed ctr: LinkedIn form opens are clicks",
        )
        _check_decimal("trend", self.trend, minimum=Decimal(-1), maximum=Decimal(1), places=8)
        _check_decimal("noise", self.noise, maximum=Decimal("0.9"), places=8)
        _check_decimal("value_per_conversion", self.value_per_conversion)
        _check_decimal(
            "landing_page_click_share",
            self.landing_page_click_share,
            maximum=Decimal(1),
            places=8,
        )
        for name in ("trend_from_offset", "active_from_offset", "active_until_offset"):
            value = getattr(self, name)
            if value is not None:
                _check_int(name, value, minimum=None)
        if self.active_from_offset is not None and self.active_until_offset is not None:
            _require(
                self.active_from_offset <= self.active_until_offset,
                "active_from_offset must not be after active_until_offset",
            )

    def is_active(self, offset: int) -> bool:
        """Whether the profile delivers on the day ``offset`` days from the anchor."""
        if self.active_from_offset is not None and offset < self.active_from_offset:
            return False
        return self.active_until_offset is None or offset <= self.active_until_offset

    def trend_factor(self, offset: int, history_start_offset: int) -> float:
        """Return the CPC multiplier of ``trend`` on day ``offset`` (at least 0.05)."""
        if self.trend == 0:
            return 1.0
        start = history_start_offset if self.trend_from_offset is None else self.trend_from_offset
        return max(0.05, 1.0 + float(self.trend) * max(0, offset - start))


@dataclass(frozen=True, slots=True)
class ExplicitDays(_Replaceable):
    """Exact metrics keyed by day offset from the anchor date (0 = the anchor's native date).

    Days absent from ``days`` have no delivery. Explicit days are authoritative: budget caps
    never rescale them, and an entity whose status is not serving still delivers them.

    Attributes:
        days: Day offset to that day's metrics, stored read-only and in offset order.
    """

    days: Mapping[int, DayMetrics] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze ``days`` in offset order and validate its keys and values.

        Raises:
            ValueError: When a key is not an integer or a value is not :class:`DayMetrics`.
        """
        items: dict[int, DayMetrics] = {}
        for offset, metrics in sorted(dict(self.days).items()):
            _check_int("ExplicitDays offset", offset, minimum=None)
            _require(isinstance(metrics, DayMetrics), "ExplicitDays values must be DayMetrics")
            items[offset] = metrics
        object.__setattr__(self, "days", MappingProxyType(items))

    def at(self, offset: int) -> DayMetrics:
        """Return the metrics of the day ``offset`` days from the anchor (zero when absent)."""
        return self.days.get(offset, _ZERO_DAY)


type Profile = DailyProfile | ExplicitDays
"""How an entity delivers: a seeded :class:`DailyProfile` or exact :class:`ExplicitDays`."""


@dataclass(frozen=True, slots=True)
class QualityScoreProfile(_Replaceable):
    """A Google keyword's quality score over time, for per-day ``historical_quality_score``.

    The score is ``score`` before ``change_offset`` and ``changed_score`` from it on (a step,
    such as the one-point drop K3 looks for). It is reported on days with impressions, on a
    ``present_share`` of them chosen by golden-ratio sampling; the current Q-G7
    ``quality_info.quality_score`` follows the same step, so a snapshot fallback sees the drop
    even when ``present_share`` is 0.

    Attributes:
        score: Quality score (1-10) before the change.
        change_offset: Day offset of the change; ``None`` = no change.
        changed_score: Quality score from ``change_offset`` on.
        present_share: Share of delivering days that report a historical quality score.
    """

    score: int
    change_offset: int | None = None
    changed_score: int | None = None
    present_share: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        """Validate scores (1-10), the change pairing and the share.

        Raises:
            ValueError: Naming the invalid parameter.
        """
        for name in ("score", "changed_score"):
            value = getattr(self, name)
            if value is not None:
                _check_int(name, value, minimum=1)
                _require(cast(int, value) <= 10, f"{name} must be <= 10")
        _require(
            (self.change_offset is None) == (self.changed_score is None),
            "change_offset and changed_score must be set together",
        )
        if self.change_offset is not None:
            _check_int("change_offset", self.change_offset, minimum=None)
        _check_decimal("present_share", self.present_share, maximum=Decimal(1), places=8)

    def score_at(self, offset: int) -> int:
        """Return the quality score on the day ``offset`` days from the anchor."""
        if (
            self.change_offset is not None
            and self.changed_score is not None
            and offset >= self.change_offset
        ):
            return self.changed_score
        return self.score


def _check_profile(name: str, profile: object) -> None:
    """Validate an optional profile field.

    Raises:
        ValueError: When the value is neither ``None`` nor a :data:`Profile`.
    """
    _require(
        profile is None or isinstance(profile, DailyProfile | ExplicitDays),
        f"{name} must be a DailyProfile, ExplicitDays or None",
    )


def _replace_keyed[T](
    items: tuple[T, ...], attribute: str, key: object, changes: Mapping[str, Any]
) -> tuple[T, ...]:
    """Return ``items`` with every item whose ``attribute`` equals ``key`` replaced.

    Raises:
        KeyError: When no item has that key.
    """
    found = False
    replaced: list[T] = []
    for item in items:
        if getattr(item, attribute) == key:
            replaced.append(cast(T, dataclasses.replace(cast(Any, item), **changes)))
            found = True
        else:
            replaced.append(item)
    if not found:
        raise KeyError(f"no item with {attribute}={key!r}")
    return tuple(replaced)


# ---------------------------------------------------------------------------
# Google specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoogleBudgetSpec(_Replaceable):
    """A Google ``campaign_budget``.

    Attributes:
        key: Spec key, unique among budgets.
        amount: The daily amount (``DAILY``) or the lifetime total (``CUSTOM_PERIOD``), in the
            account currency, at most six decimals (whole micros).
        period: ``DAILY`` or ``CUSTOM_PERIOD``.
        explicitly_shared: An explicitly shared budget, a decision subject of its own.
        name: Display name; derived from the key when ``None``.
    """

    key: str
    amount: Decimal
    period: GoogleBudgetPeriod = "DAILY"
    explicitly_shared: bool = False
    name: str | None = None

    def __post_init__(self) -> None:
        """Validate the key, amount and period."""
        _check_text("GoogleBudgetSpec.key", self.key)
        _check_decimal(f"budget {self.key} amount", self.amount, minimum=Decimal("0.000001"))
        _check_choice("period", self.period, ("DAILY", "CUSTOM_PERIOD"))


@dataclass(frozen=True, slots=True)
class GoogleCampaignSpec(_Replaceable):
    """A Google campaign.

    Its daily delivery is the sum of its keywords' traffic, its search terms without a keyword
    and its own ``profile`` (residual traffic such as display or audience delivery). A
    :class:`DailyProfile` residual is capped by a ``DAILY`` budget; ``ExplicitDays`` never is.

    Attributes:
        key: Spec key, unique among campaigns.
        name: Campaign name.
        budget_key: Key of the budget the campaign draws from.
        status: Raw ``CampaignStatusEnum``: ``ENABLED``, ``PAUSED`` or ``REMOVED``.
        channel: ``AdvertisingChannelTypeEnum``, for example ``SEARCH`` or ``DISPLAY``.
        profile: Residual delivery; ``None`` when all traffic comes from keywords.
    """

    key: str
    name: str
    budget_key: str
    status: GoogleCampaignStatus = "ENABLED"
    channel: str = "SEARCH"
    profile: Profile | None = None

    def __post_init__(self) -> None:
        """Validate the key, name, status, channel and profile."""
        _check_text("GoogleCampaignSpec.key", self.key)
        _check_text(f"campaign {self.key} name", self.name)
        _check_text(f"campaign {self.key} budget_key", self.budget_key)
        _check_choice("status", self.status, _GOOGLE_CAMPAIGN_STATUSES)
        _check_choice("channel", self.channel, _GOOGLE_CHANNELS)
        _check_profile(f"campaign {self.key} profile", self.profile)


@dataclass(frozen=True, slots=True)
class GoogleAdGroupSpec(_Replaceable):
    """A Google ad group.

    Attributes:
        key: Spec key, unique among ad groups.
        campaign_key: Key of the campaign it belongs to.
        name: Ad group name (C3b checks head tokens against it).
        status: ``ENABLED``, ``PAUSED`` or ``REMOVED``.
    """

    key: str
    campaign_key: str
    name: str
    status: GoogleCriterionStatus = "ENABLED"

    def __post_init__(self) -> None:
        """Validate the key, name and status."""
        _check_text("GoogleAdGroupSpec.key", self.key)
        _check_text(f"ad group {self.key} name", self.name)
        _check_choice("status", self.status, _GOOGLE_CAMPAIGN_STATUSES)


@dataclass(frozen=True, slots=True)
class GoogleKeywordSpec(_Replaceable):
    """A Google keyword criterion (``ad_group_criterion`` of type ``KEYWORD``).

    Its daily traffic is its own ``profile`` (the low-volume search terms Google withholds)
    plus the traffic of every search term matched to it, so search-term spend never exceeds
    keyword spend and search-term coverage is ``terms / (terms + profile)``.

    Attributes:
        key: Spec key, unique among keywords.
        ad_group_key: Key of its ad group.
        text: Keyword text.
        match_type: ``EXACT``, ``PHRASE`` or ``BROAD``.
        status: ``ENABLED``, ``PAUSED`` or ``REMOVED``.
        quality_score: Current ``quality_info.quality_score`` (1-10); ``None`` = unpopulated.
            Overridden by ``qs_profile`` when set.
        qs_profile: Quality score over time, for daily ``historical_quality_score``.
        profile: Residual delivery not covered by search terms; ``None`` = none.
    """

    key: str
    ad_group_key: str
    text: str
    match_type: GoogleMatchType = "PHRASE"
    status: GoogleCriterionStatus = "ENABLED"
    quality_score: int | None = None
    qs_profile: QualityScoreProfile | None = None
    profile: Profile | None = None

    def __post_init__(self) -> None:
        """Validate the key, text, match type, status, quality score and profiles."""
        _check_text("GoogleKeywordSpec.key", self.key)
        _check_text(f"keyword {self.key} text", self.text)
        _check_choice("match_type", self.match_type, _GOOGLE_MATCH_TYPES)
        _check_choice("status", self.status, _GOOGLE_CAMPAIGN_STATUSES)
        if self.quality_score is not None:
            _check_int("quality_score", self.quality_score, minimum=1)
            _require(self.quality_score <= 10, "quality_score must be <= 10")
        _require(
            self.qs_profile is None or isinstance(self.qs_profile, QualityScoreProfile),
            "qs_profile must be a QualityScoreProfile or None",
        )
        _check_profile(f"keyword {self.key} profile", self.profile)


@dataclass(frozen=True, slots=True)
class GoogleSearchTermSpec(_Replaceable):
    """A row family of the Google search terms report (``search_term_view``).

    Attributes:
        term: The search term, unique within its ad group.
        ad_group_key: Key of the ad group it was served in.
        matched_keyword_key: Key of the keyword it matched, in the same ad group; ``None`` for
            traffic without a keyword criterion (for example dynamic search ads), which counts
            toward the campaign but not toward any keyword.
        status: ``NONE``, ``ADDED``, ``EXCLUDED`` or ``ADDED_EXCLUDED``.
        profile: Its delivery; ``None`` = no traffic.
    """

    term: str
    ad_group_key: str
    matched_keyword_key: str | None
    status: GoogleSearchTermStatus = "NONE"
    profile: Profile | None = None

    def __post_init__(self) -> None:
        """Validate the term, status and profile."""
        _check_text("GoogleSearchTermSpec.term", self.term)
        _check_choice("status", self.status, _GOOGLE_SEARCH_TERM_STATUSES)
        _check_profile(f"search term {self.term!r} profile", self.profile)

    @property
    def key(self) -> str:
        """The term's identity: ``{ad_group_key}|{term}``."""
        return f"{self.ad_group_key}|{self.term}"


@dataclass(frozen=True, slots=True)
class GoogleConversionActionSpec(_Replaceable):
    """A Google ``conversion_action``; Q-G8 reads it with no status filter.

    Attributes:
        key: Spec key, unique among conversion actions.
        name: Display name.
        status: ``ENABLED``, ``HIDDEN`` or ``REMOVED``.
        include_in_conversions_metric: Whether it counts in ``metrics.conversions``.
        counting_type: ``ONE_PER_CLICK`` or ``MANY_PER_CLICK``.
        attribution_model: The model before any ``model_changes``.
        model_changes: ``(day offset, model)`` pairs; a change takes effect at the start of
            that native day, so the counting contract of any past day can be read.
    """

    key: str
    name: str
    status: GoogleConversionActionStatus = "ENABLED"
    include_in_conversions_metric: bool = True
    counting_type: GoogleCountingType = "ONE_PER_CLICK"
    attribution_model: str = "GOOGLE_ADS_LAST_CLICK"
    model_changes: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        """Validate the enums and the model changes (strictly increasing offsets)."""
        _check_text("GoogleConversionActionSpec.key", self.key)
        _check_text(f"conversion action {self.key} name", self.name)
        _check_choice("status", self.status, _GOOGLE_CONVERSION_ACTION_STATUSES)
        _check_choice("counting_type", self.counting_type, _GOOGLE_COUNTING_TYPES)
        _check_choice("attribution_model", self.attribution_model, _GOOGLE_ATTRIBUTION_MODELS)
        previous: int | None = None
        for offset, model in self.model_changes:
            _check_int("model change offset", offset, minimum=None)
            _check_choice("model change", model, _GOOGLE_ATTRIBUTION_MODELS)
            _require(
                previous is None or offset > previous,
                "model_changes offsets must strictly increase",
            )
            previous = offset

    def model_at(self, offset: int) -> str:
        """Return the attribution model in effect on the day ``offset`` days from the anchor."""
        model = self.attribution_model
        for change_offset, changed in self.model_changes:
            if offset >= change_offset:
                model = changed
        return model


@dataclass(frozen=True, slots=True)
class GoogleBulkSpec(_Replaceable):
    """Lazily generated filler entities for scale and pagination tests.

    Bulk entities have fixed, tiny daily metrics computed without random draws, so even
    25,000 campaigns or 15,000 keywords in one ad group cost almost nothing until read. Their
    ids follow every explicit spec id in each range.

    Attributes:
        campaigns: Bulk ``SEARCH`` campaigns, each with its own ``DAILY`` budget.
        ad_groups_per_campaign: Ad groups under each bulk campaign.
        keywords_in_one_ad_group: Keywords in one dedicated ad group (``bulk-keywords``) of a
            dedicated campaign, every one with traffic every day.
        search_term_rows_per_day: Search-term rows per day in that same ad group.
    """

    campaigns: int = 0
    ad_groups_per_campaign: int = 0
    keywords_in_one_ad_group: int = 0
    search_term_rows_per_day: int = 0

    def __post_init__(self) -> None:
        """Validate the counts (non-negative integers)."""
        for name in (
            "campaigns",
            "ad_groups_per_campaign",
            "keywords_in_one_ad_group",
            "search_term_rows_per_day",
        ):
            _check_int(name, getattr(self, name))

    @property
    def has_keyword_group(self) -> bool:
        """Whether the dedicated ``bulk-keywords`` campaign and ad group exist."""
        return self.keywords_in_one_ad_group > 0 or self.search_term_rows_per_day > 0


@dataclass(frozen=True, slots=True)
class GoogleSpec(_Replaceable):
    """The simulated Google Ads customer behind the n8n webhook.

    Attributes:
        customer_id: 10-digit customer id without dashes.
        name: ``customer.descriptive_name``.
        currency: ``customer.currency_code``.
        time_zone: ``customer.time_zone``; Google's native day. The default
            ``America/New_York`` lies behind UTC, which the C3a first-start case needs.
        budgets: Campaign budgets.
        campaigns: Campaigns.
        ad_groups: Ad groups.
        keywords: Keyword criteria.
        search_terms: Search-term report rows.
        conversion_actions: Conversion actions.
        bulk: Lazy filler for scale tests.
        emit_page_tokens: Return ``nextPageToken`` on full pages. The user-specified n8n
            response carries no token, so this is ``False`` except to exercise token following.
    """

    customer_id: str = SIM_GOOGLE_CUSTOMER_ID
    name: str = "Simulated Google Ads"
    currency: str = "USD"
    time_zone: str = "America/New_York"
    budgets: tuple[GoogleBudgetSpec, ...] = ()
    campaigns: tuple[GoogleCampaignSpec, ...] = ()
    ad_groups: tuple[GoogleAdGroupSpec, ...] = ()
    keywords: tuple[GoogleKeywordSpec, ...] = ()
    search_terms: tuple[GoogleSearchTermSpec, ...] = ()
    conversion_actions: tuple[GoogleConversionActionSpec, ...] = ()
    bulk: GoogleBulkSpec | None = None
    emit_page_tokens: bool = False

    def __post_init__(self) -> None:
        """Validate identifiers, the time zone and every cross-reference.

        Raises:
            ValueError: Naming the first broken reference or invalid value.
        """
        _require(
            isinstance(self.customer_id, str)
            and len(self.customer_id) == 10
            and self.customer_id.isdecimal(),
            f"customer_id must be 10 digits without dashes, found {self.customer_id!r}",
        )
        _check_text("Google name", self.name)
        _require(
            isinstance(self.currency, str) and len(self.currency) == 3,
            "currency must be an ISO 4217 code",
        )
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"time_zone {self.time_zone!r} is not an IANA zone") from exc
        _check_unique("budget", (budget.key for budget in self.budgets))
        _check_unique("campaign", (campaign.key for campaign in self.campaigns))
        _check_unique("ad group", (group.key for group in self.ad_groups))
        _check_unique("keyword", (keyword.key for keyword in self.keywords))
        _check_unique("search term", (term.key for term in self.search_terms))
        _check_unique("conversion action", (action.key for action in self.conversion_actions))
        budgets = {budget.key for budget in self.budgets}
        campaigns = {campaign.key for campaign in self.campaigns}
        for campaign in self.campaigns:
            _require(
                campaign.budget_key in budgets,
                f"campaign {campaign.key} names unknown budget {campaign.budget_key!r}",
            )
        groups = {group.key for group in self.ad_groups}
        for group in self.ad_groups:
            _require(
                group.campaign_key in campaigns,
                f"ad group {group.key} names unknown campaign {group.campaign_key!r}",
            )
        keyword_groups = {keyword.key: keyword.ad_group_key for keyword in self.keywords}
        for keyword in self.keywords:
            _require(
                keyword.ad_group_key in groups,
                f"keyword {keyword.key} names unknown ad group {keyword.ad_group_key!r}",
            )
        for term in self.search_terms:
            _require(
                term.ad_group_key in groups,
                f"search term {term.term!r} names unknown ad group {term.ad_group_key!r}",
            )
            if term.matched_keyword_key is not None:
                _require(
                    keyword_groups.get(term.matched_keyword_key) == term.ad_group_key,
                    f"search term {term.term!r} must match a keyword of ad group "
                    f"{term.ad_group_key!r}, found {term.matched_keyword_key!r}",
                )
        _require(
            self.bulk is None or isinstance(self.bulk, GoogleBulkSpec),
            "bulk must be a GoogleBulkSpec or None",
        )

    def with_campaign(self, key: str, **changes: Any) -> GoogleSpec:
        """Return a copy with the campaign ``key`` changed (``dataclasses.replace``)."""
        return self.with_(campaigns=_replace_keyed(self.campaigns, "key", key, changes))

    def with_budget(self, key: str, **changes: Any) -> GoogleSpec:
        """Return a copy with the budget ``key`` changed."""
        return self.with_(budgets=_replace_keyed(self.budgets, "key", key, changes))

    def with_ad_group(self, key: str, **changes: Any) -> GoogleSpec:
        """Return a copy with the ad group ``key`` changed."""
        return self.with_(ad_groups=_replace_keyed(self.ad_groups, "key", key, changes))

    def with_keyword(self, key: str, **changes: Any) -> GoogleSpec:
        """Return a copy with the keyword ``key`` changed."""
        return self.with_(keywords=_replace_keyed(self.keywords, "key", key, changes))

    def with_search_term(self, term: str, **changes: Any) -> GoogleSpec:
        """Return a copy with the search term ``term`` changed (every ad group holding it)."""
        return self.with_(search_terms=_replace_keyed(self.search_terms, "term", term, changes))

    def with_conversion_action(self, key: str, **changes: Any) -> GoogleSpec:
        """Return a copy with the conversion action ``key`` changed."""
        return self.with_(
            conversion_actions=_replace_keyed(self.conversion_actions, "key", key, changes)
        )


# ---------------------------------------------------------------------------
# LinkedIn specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkedInAccountSpec(_Replaceable):
    """A LinkedIn ad account (``urn:li:sponsoredAccount:{id}``).

    Attributes:
        id: Account id from ``LINKEDIN_ACCOUNT_IDS``.
        name: Account name.
        currency: Account currency; amounts are never converted across accounts.
    """

    id: int
    name: str
    currency: str = "USD"

    def __post_init__(self) -> None:
        """Validate the reserved id, name and currency."""
        _require(
            LINKEDIN_ACCOUNT_IDS.contains(self.id),
            f"LinkedIn account id {self.id} lies outside LINKEDIN_ACCOUNT_IDS",
        )
        _check_text(f"account {self.id} name", self.name)
        _require(
            isinstance(self.currency, str) and len(self.currency) == 3,
            "currency must be an ISO 4217 code",
        )


@dataclass(frozen=True, slots=True)
class LinkedInCampaignGroupSpec(_Replaceable):
    """A LinkedIn campaign group. Group status does not gate delivery in the simulator.

    Attributes:
        key: Spec key, unique among campaign groups.
        account_id: Owning account id.
        name: Group name.
        status: One of the group statuses (groups have no ``COMPLETED``).
        daily_budget: Optional group daily budget.
        total_budget: Optional group total budget.
        run_start_offset: Day offset of the run schedule start; history start when ``None``.
        run_end_offset: Day offset of the run schedule end; open-ended when ``None``.
    """

    key: str
    account_id: int
    name: str
    status: LinkedInCampaignGroupStatus = "ACTIVE"
    daily_budget: Decimal | None = None
    total_budget: Decimal | None = None
    run_start_offset: int | None = None
    run_end_offset: int | None = None

    def __post_init__(self) -> None:
        """Validate the key, status, budgets and schedule."""
        _check_text("LinkedInCampaignGroupSpec.key", self.key)
        _check_text(f"campaign group {self.key} name", self.name)
        _check_choice("status", self.status, _LINKEDIN_GROUP_STATUSES)
        for name in ("daily_budget", "total_budget"):
            value = getattr(self, name)
            if value is not None:
                _check_decimal(f"campaign group {self.key} {name}", value, places=2)
        for name in ("run_start_offset", "run_end_offset"):
            value = getattr(self, name)
            if value is not None:
                _check_int(name, value, minimum=None)


@dataclass(frozen=True, slots=True)
class LinkedInCampaignSpec(_Replaceable):
    """A LinkedIn campaign.

    A ``daily_budget`` caps its creatives' delivery: when their summed demand exceeds it, the
    campaign spends a drawn 96-100% of the budget that complete day (about 98% on average), so
    the day meets the 0.95 ``budget.capped_day_ratio``. ``ExplicitDays`` creatives are
    delivered as stated; the :class:`DailyProfile` creatives are scaled down, rounded to whole
    impressions and clicks, and priced at the rest of that spend over what they bought (clicks,
    or impressions when the budget buys no click), so rounding moves the cost per click, never
    the spend. Two days fall outside the range: one whose explicit spend alone reaches the drawn
    share spends just that, and one whose budget buys no impression spends only the explicit
    spend. The current day is partial: it spends the elapsed share of its full day.

    Attributes:
        key: Spec key, unique among campaigns.
        account_id: Owning account id.
        group_key: Key of its campaign group (same account).
        name: Campaign name.
        status: One of the campaign statuses, ``COMPLETED`` included.
        objective: ``objectiveType``, for example ``LEAD_GENERATION`` or
            ``WEBSITE_CONVERSION``.
        daily_budget: Daily budget in the account currency (minor units), or ``None``.
        total_budget: Lifetime budget in the account currency, or ``None``.
    """

    key: str
    account_id: int
    group_key: str
    name: str
    status: LinkedInCampaignStatus = "ACTIVE"
    objective: str = "LEAD_GENERATION"
    daily_budget: Decimal | None = None
    total_budget: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate the key, status, objective and budgets."""
        _check_text("LinkedInCampaignSpec.key", self.key)
        _check_text(f"campaign {self.key} name", self.name)
        _check_choice("status", self.status, _LINKEDIN_CAMPAIGN_STATUSES)
        _check_choice("objective", self.objective, _LINKEDIN_OBJECTIVES)
        for name in ("daily_budget", "total_budget"):
            value = getattr(self, name)
            if value is not None:
                _check_decimal(f"campaign {self.key} {name}", value, places=2)


@dataclass(frozen=True, slots=True)
class LinkedInCreativeSpec(_Replaceable):
    """A LinkedIn creative (sponsored content).

    Attributes:
        key: Spec key, unique among creatives.
        campaign_key: Key of its campaign.
        headline: Headline of the referenced post.
        intro_text: Intro (commentary) text of the referenced post.
        id: Explicit creative id, required when it must equal a URN of
            ``config/content_assets.yaml``; allocated from ``LINKEDIN_CREATIVE_IDS`` otherwise.
        intended_status: ``intendedStatus``.
        cta_label: Lead-gen call-to-action label (a ``leadgenCallToAction.label`` value).
        lead_form_id: Canonical lead form id of its call-to-action; ``None`` for landing-page
            creatives, which then report landing-page clicks and website conversions.
        landing_page_url: Destination of a landing-page creative.
        profile: Its delivery.
    """

    key: str
    campaign_key: str
    headline: str
    intro_text: str
    id: int | None = None
    intended_status: LinkedInCreativeStatus = "ACTIVE"
    cta_label: LinkedInCtaLabel = "DOWNLOAD"
    lead_form_id: int | None = None
    landing_page_url: str | None = None
    profile: Profile | None = None

    def __post_init__(self) -> None:
        """Validate the key, id, texts, status, label and profile."""
        _check_text("LinkedInCreativeSpec.key", self.key)
        _check_text(f"creative {self.key} headline", self.headline)
        _check_text(f"creative {self.key} intro_text", self.intro_text)
        if self.id is not None:
            _require(
                LINKEDIN_CREATIVE_IDS.contains(self.id),
                f"creative id {self.id} lies outside LINKEDIN_CREATIVE_IDS",
            )
        _check_choice("intended_status", self.intended_status, _LINKEDIN_CREATIVE_STATUSES)
        _check_choice("cta_label", self.cta_label, _LINKEDIN_CTA_LABELS)
        if self.lead_form_id is not None:
            _require(
                LINKEDIN_FORM_IDS.contains(self.lead_form_id),
                f"creative {self.key} lead_form_id lies outside LINKEDIN_FORM_IDS",
            )
        _check_profile(f"creative {self.key} profile", self.profile)


@dataclass(frozen=True, slots=True)
class QuestionSpec(_Replaceable):
    """One question of a lead form version.

    Attributes:
        question_id: Question id (Long), unique within the version.
        name: Machine name, for example ``email``.
        predefined_field: Profile field that pre-fills it, for example ``EMAIL`` or
            ``WORK_EMAIL``; ``None`` for a custom question.
        text: Question text shown to the member; derived from ``name`` when ``None``.
    """

    question_id: int
    name: str
    predefined_field: str | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        """Validate the id and name."""
        _check_int("question_id", self.question_id, minimum=1)
        _check_text("question name", self.name)


@dataclass(frozen=True, slots=True)
class FormVersionSpec(_Replaceable):
    """One version of a lead form.

    Editing a form creates a new version, in which a question id can change meaning
    (AAP 0.2.4).

    Attributes:
        version: Version number.
        questions: Questions in display order.
        active_from_offset: Day offset from which new responses use this version; the first
            history day when ``None``.
    """

    version: int
    questions: tuple[QuestionSpec, ...]
    active_from_offset: int | None = None

    def __post_init__(self) -> None:
        """Validate the version, the questions and their unique ids."""
        _check_int("version", self.version, minimum=1)
        _require(len(self.questions) > 0, f"form version {self.version} needs questions")
        _check_unique("question id", (question.question_id for question in self.questions))
        if self.active_from_offset is not None:
            _check_int("active_from_offset", self.active_from_offset, minimum=None)


@dataclass(frozen=True, slots=True)
class LinkedInLeadFormSpec(_Replaceable):
    """A LinkedIn lead gen form (``urn:li:leadGenForm:{id}``), owned by one account.

    Attributes:
        id: Canonical form id from ``LINKEDIN_FORM_IDS``; the white paper registry joins on it.
        account_id: Owning (sponsored) account.
        name: Form name.
        versions: Versions in increasing version and activation order.
    """

    id: int
    account_id: int
    name: str
    versions: tuple[FormVersionSpec, ...]

    def __post_init__(self) -> None:
        """Validate the id, name and version ordering."""
        _require(
            LINKEDIN_FORM_IDS.contains(self.id),
            f"lead form id {self.id} lies outside LINKEDIN_FORM_IDS",
        )
        _check_text(f"lead form {self.id} name", self.name)
        _require(len(self.versions) > 0, f"lead form {self.id} needs at least one version")
        previous: FormVersionSpec | None = None
        for version in self.versions:
            if previous is not None:
                _require(
                    version.version > previous.version,
                    f"lead form {self.id} versions must strictly increase",
                )
                _require(
                    version.active_from_offset is not None
                    and (
                        previous.active_from_offset is None
                        or version.active_from_offset > previous.active_from_offset
                    ),
                    f"lead form {self.id} version {version.version} must activate after "
                    f"version {previous.version}",
                )
            previous = version


@dataclass(frozen=True, slots=True)
class SegmentValueSpec(_Replaceable):
    """One demographic value's share of a campaign's delivery.

    Attributes:
        value: The value URN, for example ``urn:li:seniority:6`` (Director).
        impression_share: Share of the campaign's impressions, clicks and spend.
        cpl_factor: Cost per lead of the value relative to the campaign's (0.5 = half).
    """

    value: str
    impression_share: Decimal
    cpl_factor: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        """Validate the share and factor."""
        _check_text("segment value", self.value)
        _check_decimal(
            f"segment {self.value} impression_share",
            self.impression_share,
            maximum=Decimal(1),
            places=8,
        )
        _check_decimal(
            f"segment {self.value} cpl_factor", self.cpl_factor, minimum=Decimal("0.01"), places=8
        )


@dataclass(frozen=True, slots=True)
class LinkedInSegmentSpec(_Replaceable):
    """An explicit demographic distribution of one campaign under one pivot.

    The listed values take their shares; the rest of the pivot's default catalogue shares the
    remaining impressions, with a cost per lead set so the campaign's lead total is kept.

    Attributes:
        campaign_key: Key of the campaign.
        pivot: ``MEMBER_JOB_FUNCTION``, ``MEMBER_SENIORITY`` or ``MEMBER_INDUSTRY``.
        values: Explicit values; their impression shares sum to at most 1.
        leads_available: Whether the pivot returns lead metrics; ``False`` leaves leads and
            website conversions unavailable (``None``).
    """

    campaign_key: str
    pivot: SegmentPivot
    values: tuple[SegmentValueSpec, ...] = ()
    leads_available: bool = True

    def __post_init__(self) -> None:
        """Validate the pivot, value URN prefixes and the share total."""
        _check_text("segment campaign_key", self.campaign_key)
        _check_choice("pivot", self.pivot, SEGMENT_PIVOTS)
        prefix = _SEGMENT_VALUE_PREFIXES[self.pivot]
        for value in self.values:
            _require(
                value.value.startswith(prefix),
                f"segment value {value.value!r} must start with {prefix} for {self.pivot}",
            )
        _check_unique("segment value", (value.value for value in self.values))
        total = sum((value.impression_share for value in self.values), Decimal(0))
        _require(total <= 1, f"segment shares of {self.campaign_key}/{self.pivot} exceed 1")


@dataclass(frozen=True, slots=True)
class LinkedInSpec(_Replaceable):
    """The simulated LinkedIn advertiser: accounts, entities, forms, leads and segments.

    Attributes:
        accounts: Ad accounts.
        campaign_groups: Campaign groups.
        campaigns: Campaigns.
        creatives: Creatives.
        lead_forms: Lead gen forms (the white paper registry's forms).
        segments: Explicit demographic distributions.
        test_lead_share: Share of lead responses that are test leads (``testLead=true``),
            which ingest drops; they are not counted in daily ``leads``.
        unknown_creative_share: Share of lead responses that carry no associated creative,
            to exercise the "unknown creative" row and creative floor.
        analytics_next_links: Return ``next`` paging links on capped analytics responses;
            ``False`` truncates at 15,000 elements with no link.
        segment_min_impressions: Privacy threshold: demographic values below it are omitted.
        segment_top_values: At most this many values per (campaign, pivot) are returned.
    """

    accounts: tuple[LinkedInAccountSpec, ...] = ()
    campaign_groups: tuple[LinkedInCampaignGroupSpec, ...] = ()
    campaigns: tuple[LinkedInCampaignSpec, ...] = ()
    creatives: tuple[LinkedInCreativeSpec, ...] = ()
    lead_forms: tuple[LinkedInLeadFormSpec, ...] = ()
    segments: tuple[LinkedInSegmentSpec, ...] = ()
    test_lead_share: Decimal = Decimal(0)
    unknown_creative_share: Decimal = Decimal(0)
    analytics_next_links: bool = False
    segment_min_impressions: int = 100
    segment_top_values: int = 100

    def __post_init__(self) -> None:
        """Validate uniqueness and every cross-reference.

        Raises:
            ValueError: Naming the first broken reference or invalid value.
        """
        _check_unique("LinkedIn account", (account.id for account in self.accounts))
        _check_unique("campaign group", (group.key for group in self.campaign_groups))
        _check_unique("campaign", (campaign.key for campaign in self.campaigns))
        _check_unique("creative", (creative.key for creative in self.creatives))
        _check_unique("lead form", (form.id for form in self.lead_forms))
        _check_unique(
            "segment", ((segment.campaign_key, segment.pivot) for segment in self.segments)
        )
        _check_unique(
            "creative id",
            (creative.id for creative in self.creatives if creative.id is not None),
        )
        _check_decimal("test_lead_share", self.test_lead_share, maximum=Decimal("0.5"), places=8)
        _check_decimal(
            "unknown_creative_share", self.unknown_creative_share, maximum=Decimal(1), places=8
        )
        _check_int("segment_min_impressions", self.segment_min_impressions)
        _check_int("segment_top_values", self.segment_top_values, minimum=1)
        accounts = {account.id for account in self.accounts}
        group_accounts: dict[str, int] = {}
        for group in self.campaign_groups:
            _require(
                group.account_id in accounts,
                f"campaign group {group.key} names unknown account {group.account_id}",
            )
            group_accounts[group.key] = group.account_id
        campaign_accounts: dict[str, int] = {}
        for campaign in self.campaigns:
            _require(
                group_accounts.get(campaign.group_key) == campaign.account_id,
                f"campaign {campaign.key} must name a campaign group of account "
                f"{campaign.account_id}, found {campaign.group_key!r}",
            )
            campaign_accounts[campaign.key] = campaign.account_id
        form_accounts: dict[int, int] = {}
        for form in self.lead_forms:
            _require(
                form.account_id in accounts,
                f"lead form {form.id} names unknown account {form.account_id}",
            )
            form_accounts[form.id] = form.account_id
        for creative in self.creatives:
            account = campaign_accounts.get(creative.campaign_key)
            _require(
                account is not None,
                f"creative {creative.key} names unknown campaign {creative.campaign_key!r}",
            )
            if creative.lead_form_id is not None:
                _require(
                    form_accounts.get(creative.lead_form_id) == account,
                    f"creative {creative.key} must use a lead form of account {account}, "
                    f"found {creative.lead_form_id}",
                )
        for segment in self.segments:
            _require(
                segment.campaign_key in campaign_accounts,
                f"segment names unknown campaign {segment.campaign_key!r}",
            )

    def with_campaign(self, key: str, **changes: Any) -> LinkedInSpec:
        """Return a copy with the campaign ``key`` changed (``dataclasses.replace``)."""
        return self.with_(campaigns=_replace_keyed(self.campaigns, "key", key, changes))

    def with_campaign_group(self, key: str, **changes: Any) -> LinkedInSpec:
        """Return a copy with the campaign group ``key`` changed."""
        return self.with_(campaign_groups=_replace_keyed(self.campaign_groups, "key", key, changes))

    def with_creative(self, key: str, **changes: Any) -> LinkedInSpec:
        """Return a copy with the creative ``key`` changed."""
        return self.with_(creatives=_replace_keyed(self.creatives, "key", key, changes))

    def with_lead_form(self, form_id: int, **changes: Any) -> LinkedInSpec:
        """Return a copy with the lead form ``form_id`` changed."""
        return self.with_(lead_forms=_replace_keyed(self.lead_forms, "id", form_id, changes))


# ---------------------------------------------------------------------------
# HubSpot specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageSpec(_Replaceable):
    """A custom HubSpot lifecycle stage; custom stages have numeric ids.

    Attributes:
        label: Stage label, matched exactly by ``config/hubspot_mapping.yaml``.
        id: Numeric stage id (the option value) from ``HUBSPOT_STAGE_IDS``.
    """

    label: str
    id: str

    def __post_init__(self) -> None:
        """Validate the label and that the id lies in ``HUBSPOT_STAGE_IDS``."""
        _check_text("stage label", self.label)
        _require(
            HUBSPOT_STAGE_IDS.contains(self.id),
            f"custom stage id {self.id!r} lies outside HUBSPOT_STAGE_IDS",
        )


def _default_custom_stages() -> tuple[StageSpec, ...]:
    """The default custom stages: "First meeting booked" (MQL) and "Qualified Lead" (QL)."""
    return (
        StageSpec("First meeting booked", str(HUBSPOT_STAGE_IDS.at(0))),
        StageSpec("Qualified Lead", str(HUBSPOT_STAGE_IDS.at(1))),
    )


def _check_window(name: str, window: tuple[int, int]) -> None:
    """Validate an ``(minimum, maximum)`` day window."""
    _require(
        len(window) == 2 and 0 <= window[0] <= window[1],
        f"{name} must be (minimum, maximum) with 0 <= minimum <= maximum",
    )


@dataclass(frozen=True, slots=True)
class FunnelSpec(_Replaceable):
    """How simulated people move through the CRM after their LinkedIn touch.

    Every share is drawn per person from the person's own generator. Delays are inclusive
    ``(minimum, maximum)`` days after the person's first touch. MQL and QL are drawn
    independently (QL is never derived from MQL); a skipped stage leaves MQL's date empty
    although the contact reaches QL.

    Attributes:
        match_share: Share of people who have (or get) a HubSpot contact.
        late_contact_share: Share of contacts created only ``late_contact_delay`` after the
            touch, so lead matching finds them on a retry.
        late_contact_delay: Delay of a late contact's creation.
        mql_share: Share of contacts that enter the MQL-bound stage.
        ql_share: Share of contacts that enter the QL-bound stage.
        skipped_stage_share: Share of QL contacts whose MQL stage is skipped (empty date).
        pre_touch_share: Share of contacts that already existed and were MQL/QL before the
            touch ("already MQL/QL before this touch").
        meeting_share: Share of contacts with a meeting.
        meeting_sources: ``(hs_meeting_source, weight)`` pairs; ``MEETINGS_PUBLIC`` is a
            scheduling-page booking, the others are logged, synced or imported meetings.
        canceled_share: Share of meetings with outcome ``CANCELED``.
        meeting_created_after_start_share: Share of meetings created after the touch but
            starting before it (logged retroactively).
        two_contact_meeting_share: Share of meetings also associated with a second contact.
        deal_share: Share of contacts with a deal created after the touch.
        pre_touch_deal_share: Share of contacts with a deal created before the touch.
        repeat_person_share: Share of leads submitted by someone who already submitted one.
        mql_delay_days: MQL stage entry delay after the touch.
        ql_delay_days: QL stage entry delay after the touch.
        meeting_delay_days: Meeting creation delay after the touch.
        deal_delay_days: Deal creation delay after the touch.
    """

    match_share: Decimal = Decimal("0.975")
    late_contact_share: Decimal = Decimal("0.02")
    late_contact_delay: timedelta = timedelta(days=2)
    mql_share: Decimal = Decimal("0.40")
    ql_share: Decimal = Decimal("0.22")
    skipped_stage_share: Decimal = Decimal("0.10")
    pre_touch_share: Decimal = Decimal("0.03")
    meeting_share: Decimal = Decimal("0.30")
    meeting_sources: tuple[tuple[str, Decimal], ...] = (
        ("MEETINGS_PUBLIC", Decimal("0.70")),
        ("CRM_UI", Decimal("0.20")),
        ("INTEGRATION", Decimal("0.10")),
    )
    canceled_share: Decimal = Decimal("0.08")
    meeting_created_after_start_share: Decimal = Decimal("0.03")
    two_contact_meeting_share: Decimal = Decimal("0.03")
    deal_share: Decimal = Decimal("0.08")
    pre_touch_deal_share: Decimal = Decimal("0.02")
    repeat_person_share: Decimal = Decimal("0.03")
    mql_delay_days: tuple[int, int] = (1, 12)
    ql_delay_days: tuple[int, int] = (4, 25)
    meeting_delay_days: tuple[int, int] = (1, 14)
    deal_delay_days: tuple[int, int] = (5, 40)

    def __post_init__(self) -> None:
        """Validate every share (in [0, 1]), the delay windows and the source weights."""
        for name in (
            "match_share",
            "late_contact_share",
            "mql_share",
            "ql_share",
            "skipped_stage_share",
            "pre_touch_share",
            "meeting_share",
            "canceled_share",
            "meeting_created_after_start_share",
            "two_contact_meeting_share",
            "deal_share",
            "pre_touch_deal_share",
            "repeat_person_share",
        ):
            _check_decimal(name, getattr(self, name), maximum=Decimal(1), places=8)
        _require(
            isinstance(self.late_contact_delay, timedelta)
            and self.late_contact_delay >= timedelta(0),
            "late_contact_delay must be a non-negative timedelta",
        )
        _require(len(self.meeting_sources) > 0, "meeting_sources needs at least one source")
        for source, weight in self.meeting_sources:
            _check_choice("meeting source", source, _MEETING_SOURCES)
            _check_decimal(f"meeting source {source} weight", weight, places=8)
        _require(
            sum((weight for _source, weight in self.meeting_sources), Decimal(0)) > 0,
            "meeting_sources weights must not all be zero",
        )
        for name in ("mql_delay_days", "ql_delay_days", "meeting_delay_days", "deal_delay_days"):
            _check_window(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class BulkDealSpec(_Replaceable):
    """Many orphan deals created on one UTC day, to overflow CRM Search partitions.

    Attributes:
        day_offset: Day offset of the deals' creation date.
        count: Deals created that day.
        peak_hour: UTC hour (0-23) holding ``peak_hour_count`` of them.
        peak_hour_count: Deals in the peak hour (above 10,000 makes that hour overflow).
    """

    day_offset: int
    count: int
    peak_hour: int = 12
    peak_hour_count: int = 0

    def __post_init__(self) -> None:
        """Validate the counts and the hour."""
        _check_int("day_offset", self.day_offset, minimum=None)
        _check_int("count", self.count, minimum=1)
        _check_int("peak_hour", self.peak_hour)
        _require(self.peak_hour <= 23, "peak_hour must be 0-23")
        _check_int("peak_hour_count", self.peak_hour_count)
        _require(self.peak_hour_count <= self.count, "peak_hour_count cannot exceed count")


@dataclass(frozen=True, slots=True)
class HubSpotSpec(_Replaceable):
    """The simulated HubSpot portal: lifecycle stages, funnel and CRM-side behaviour.

    Attributes:
        custom_stages: Custom lifecycle stages added to HubSpot's defaults.
        removed_stage_labels: Labels deliberately absent from the portal, so a mapping that
            names them stays unresolved ("unavailable", never 0). Any other label the mapping
            names but the portal lacks is added as a custom stage.
        funnel: How people become contacts, MQL, QL, meetings and deals.
        orphan_deals: Deals with no associated contact, created over the recent history.
        bulk_deals: Lazily generated deals that overflow CRM Search partitions.
        association_page_size: Associations per page of an association batch read.
        live_lead_match_share: Share of real lead emails (HubSpot simulated, LinkedIn live)
            answered by seeded contacts.
        google_crm_properties: ``(campaign property, touch-date property)`` of the Google
            CRM link; ``None`` = no Google link (Google items carry ``no_crm_link``).
        google_touch_contacts: Contacts discovered through the Google touch-date property.
        deal_currency: Currency of deal amounts.
    """

    custom_stages: tuple[StageSpec, ...] = field(default_factory=_default_custom_stages)
    removed_stage_labels: tuple[str, ...] = ()
    funnel: FunnelSpec = field(default_factory=FunnelSpec)
    orphan_deals: int = 0
    bulk_deals: BulkDealSpec | None = None
    association_page_size: int = 500
    live_lead_match_share: Decimal = Decimal("0.6")
    google_crm_properties: tuple[str, str] | None = None
    google_touch_contacts: int = 0
    deal_currency: str = "USD"

    def __post_init__(self) -> None:
        """Validate the stages, counts and shares."""
        _check_unique("custom stage label", (stage.label for stage in self.custom_stages))
        _check_unique("custom stage id", (stage.id for stage in self.custom_stages))
        default_labels = {label for _value, label in DEFAULT_LIFECYCLE_STAGES}
        for stage in self.custom_stages:
            _require(
                stage.label not in default_labels,
                f"custom stage {stage.label!r} repeats a default stage label",
            )
        for label in self.removed_stage_labels:
            _check_text("removed stage label", label)
        _check_int("orphan_deals", self.orphan_deals)
        _check_int("association_page_size", self.association_page_size, minimum=1)
        _check_decimal(
            "live_lead_match_share", self.live_lead_match_share, maximum=Decimal(1), places=4
        )
        if self.google_crm_properties is not None:
            _require(
                len(self.google_crm_properties) == 2,
                "google_crm_properties must be (campaign property, touch-date property)",
            )
            for name in self.google_crm_properties:
                _check_text("Google CRM property", name)
        _check_int("google_touch_contacts", self.google_touch_contacts)
        _require(
            isinstance(self.deal_currency, str) and len(self.deal_currency) == 3,
            "deal_currency must be an ISO 4217 code",
        )

    def with_funnel(self, **changes: Any) -> HubSpotSpec:
        """Return a copy with the funnel changed (``dataclasses.replace``)."""
        return self.with_(funnel=self.funnel.with_(**changes))


# ---------------------------------------------------------------------------
# Faults and platform changes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fault(_Replaceable):
    """A deliberate misbehaviour of one simulated surface, applied by ``payloads.render``.

    Kinds:

    * ``http_status``: the response is ``status`` with the platform's error body (``code``,
      ``message``); ``retry_after`` sets ``Retry-After``.
    * ``timeout``: the request times out.
    * ``body_error``: Google only; HTTP 200 whose body carries a Google error ``code`` such as
      ``PERMISSION_DENIED``, ``UNAUTHENTICATED``, ``RESOURCE_EXHAUSTED`` or ``QueryError``
      (n8n relays errors inside 200).
    * ``batch_errors``: the listed ``ids`` fail inside a batch (HubSpot 207 ``errors[]``,
      LinkedIn batch ``errors``) with ``category`` and ``sub_category``.
    * ``omit_ids``: the listed ``ids`` are silently missing from a batch result (unread
      meeting or deal targets, a missing contact refresh, "email no longer retrievable").
    * ``continuation_failure``: HubSpot association continuation of the listed ``ids`` fails.
    * ``non_complete_status``: a HubSpot batch returns ``batch_status`` instead of
      ``COMPLETE``.
    * ``decoration_mismatch`` / ``no_decoration``: LinkedIn lead responses ``ids`` are
      decorated with another version's questions, or undecorated with an unknown version.

    Attributes:
        source: ``google``, ``linkedin`` or ``hubspot``.
        surface: A surface of :data:`FAULT_SURFACES` for the source.
        kind: One of :data:`FAULT_KINDS`.
        status: HTTP status (``http_status``; 400-599).
        code: Platform error code (``http_status``, ``body_error``).
        message: Error message; a platform-style default when ``None``.
        retry_after: ``Retry-After`` seconds for ``http_status``.
        ids: Affected ids (batch, omission, continuation and decoration kinds).
        category: Error category of ``batch_errors``, for example ``OBJECT_NOT_FOUND``.
        sub_category: Error sub-category of ``batch_errors``.
        batch_status: Batch status of ``non_complete_status``.
        match: Request predicates that must all hold, for example ``{"query_id": "Q-G3"}``,
            ``{"object_type": "deals"}``, ``{"page": "2"}`` or ``{"account_id": "990000001"}``.
        times: Apply to the first ``times`` matching requests, then behave normally;
            ``None`` = every matching request.
    """

    source: Source
    surface: str
    kind: FaultKind
    status: int | None = None
    code: str | None = None
    message: str | None = None
    retry_after: int | None = None
    ids: tuple[str, ...] = ()
    category: str | None = None
    sub_category: str | None = None
    batch_status: str | None = None
    match: Mapping[str, str] = field(default_factory=dict)
    times: int | None = None

    def __post_init__(self) -> None:
        """Validate the source, surface, kind and the fields each kind needs.

        Raises:
            ValueError: Naming the missing or invalid field.
        """
        _check_choice("Fault.source", self.source, SOURCES)
        _check_choice(f"Fault.surface for {self.source}", self.surface, FAULT_SURFACES[self.source])
        _check_choice("Fault.kind", self.kind, FAULT_KINDS)
        object.__setattr__(self, "match", _frozen({str(k): str(v) for k, v in self.match.items()}))
        object.__setattr__(self, "ids", tuple(str(value) for value in self.ids))
        if self.times is not None:
            _check_int("Fault.times", self.times, minimum=1)
        if self.retry_after is not None:
            _check_int("Fault.retry_after", self.retry_after)
        kind = self.kind
        if kind == "http_status":
            _require(
                self.status is not None and 400 <= self.status <= 599,
                "an http_status fault needs a status between 400 and 599",
            )
        elif kind == "body_error":
            _require(self.source == "google", "body_error faults are Google (n8n) faults")
            _require(self.code is not None, "a body_error fault needs a Google error code")
        elif kind == "batch_errors":
            _require(bool(self.ids), "a batch_errors fault needs ids")
            _require(self.category is not None, "a batch_errors fault needs a category")
        elif kind in ("omit_ids", "continuation_failure", "decoration_mismatch", "no_decoration"):
            _require(bool(self.ids), f"a {kind} fault needs ids")
        elif kind == "non_complete_status":
            _check_choice("batch_status", self.batch_status, ("CANCELED", "PENDING", "PROCESSING"))
        if kind == "continuation_failure":
            _require(
                self.surface == "associations_batch_read",
                "continuation_failure applies to associations_batch_read",
            )
        if kind in ("decoration_mismatch", "no_decoration"):
            _require(
                self.surface in ("lead_form_responses", "lead_form_responses_batch"),
                f"{kind} applies to lead form responses",
            )

    def matches(self, attributes: Mapping[str, str]) -> bool:
        """Whether every ``match`` predicate holds for a request's ``attributes``."""
        return all(attributes.get(name) == value for name, value in self.match.items())


type ChangeEntity = Literal[
    "campaign", "budget", "ad_group", "keyword", "campaign_group", "creative"
]
type ChangeField = Literal["status", "amount", "daily_budget", "total_budget", "budget_key"]

# Fields each (source, entity) accepts in a PlatformChange.
_CHANGE_FIELDS: Final[Mapping[tuple[str, str], tuple[str, ...]]] = MappingProxyType(
    {
        ("google", "campaign"): ("status", "budget_key"),
        ("google", "budget"): ("amount",),
        ("google", "ad_group"): ("status",),
        ("google", "keyword"): ("status",),
        ("linkedin", "campaign"): ("status", "daily_budget", "total_budget"),
        ("linkedin", "campaign_group"): ("status", "daily_budget", "total_budget"),
        ("linkedin", "creative"): ("status",),
    }
)


@dataclass(frozen=True, slots=True)
class PlatformChange(_Replaceable):
    """A change made on the platform at an instant, as an operator would make it.

    Entity reads at instant ``t`` (``SimulatedWorld.state_at``) apply every change at or
    before ``t``, in instant order; budget caps and delivery follow the change from the native
    day it falls in. Supports C0 cases (a budget differing by one micro, a status change, a
    member moved to another shared budget, a ``missing`` entity and its reappearance after a
    tombstone) and budget-day cases such as "a budget raised at noon".

    Attributes:
        source: ``google`` or ``linkedin``.
        entity: ``campaign``, ``budget``, ``ad_group``, ``keyword``, ``campaign_group`` or
            ``creative``.
        key: Spec key of the entity.
        field: Changed field: ``status``, ``amount`` (Google budget), ``daily_budget``,
            ``total_budget`` (LinkedIn) or ``budget_key`` (Google campaign moved to another
            budget); ``None`` with ``disappear``.
        value: New value: a status string, a ``Decimal`` amount (``None`` clears a LinkedIn
            budget) or a budget key.
        at_offset_hours: Hours after the anchor's UTC midnight (negative = before).
        at: Absolute aware instant, instead of ``at_offset_hours``.
        disappear: The entity is no longer returned by any listing from this instant.
        reappear_at: Absolute instant at which a disappeared entity is listed again.
        reappear_at_offset_hours: Hours after the anchor's UTC midnight of the reappearance.
    """

    source: Source
    entity: ChangeEntity
    key: str
    field: ChangeField | None = None
    value: str | Decimal | None = None
    at_offset_hours: int | None = None
    at: datetime | None = None
    disappear: bool = False
    reappear_at: datetime | None = None
    reappear_at_offset_hours: int | None = None

    def __post_init__(self) -> None:
        """Validate the instant, the entity and field pairing and the value type.

        Raises:
            ValueError: Naming the invalid combination.
        """
        allowed = _CHANGE_FIELDS.get((self.source, self.entity))
        _require(
            allowed is not None,
            f"platform changes do not support {self.source} {self.entity} entities",
        )
        _check_text("PlatformChange.key", self.key)
        _require(
            (self.at is None) != (self.at_offset_hours is None),
            "a platform change needs exactly one of at and at_offset_hours",
        )
        if self.at is not None:
            object.__setattr__(self, "at", _require_aware("PlatformChange.at", self.at))
        if self.at_offset_hours is not None:
            _check_int("at_offset_hours", self.at_offset_hours, minimum=None)
        if self.disappear:
            _require(self.field is None, "a disappearance changes no field")
        else:
            _require(
                self.field is not None and self.field in cast(tuple[str, ...], allowed),
                f"{self.source} {self.entity} changes accept {', '.join(allowed or ())}",
            )
            _require(
                self.reappear_at is None and self.reappear_at_offset_hours is None,
                "only a disappearance can reappear",
            )
            if self.field in ("status", "budget_key"):
                _check_text(f"{self.field} value", self.value)
            elif self.field == "amount":
                _check_decimal("amount", self.value, minimum=Decimal("0.000001"))
            elif self.value is not None:
                _check_decimal(str(self.field), self.value, places=2)
        _require(
            self.reappear_at is None or self.reappear_at_offset_hours is None,
            "set at most one of reappear_at and reappear_at_offset_hours",
        )
        if self.reappear_at is not None:
            object.__setattr__(self, "reappear_at", _require_aware("reappear_at", self.reappear_at))

    def instant(self, anchor: date) -> datetime:
        """Return the change's UTC instant for a world anchored at ``anchor``."""
        if self.at is not None:
            return self.at
        return _midnight(anchor) + timedelta(hours=cast(int, self.at_offset_hours))

    def reappear_instant(self, anchor: date) -> datetime | None:
        """Return the reappearance instant, or ``None`` when the entity stays absent."""
        if self.reappear_at is not None:
            return self.reappear_at
        if self.reappear_at_offset_hours is not None:
            return _midnight(anchor) + timedelta(hours=self.reappear_at_offset_hours)
        return None


# ---------------------------------------------------------------------------
# The world specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorldSpec(_Replaceable):
    """Everything a :class:`SimulatedWorld` is built from; ``None`` parts are absent sources.

    Attributes:
        name: Scenario or world name, logged with the build.
        anchor: The date day offsets are relative to; the clock's current UTC date when
            ``None``.
        history_days: Days of history before the anchor (at least ``BACKFILL_DAYS`` plus the
            restatement windows).
        google: The Google Ads customer.
        linkedin: The LinkedIn advertiser.
        hubspot: The HubSpot portal.
        faults: Deliberate misbehaviours.
        platform_changes: Changes made on the platforms over time.
    """

    name: str
    anchor: date | None = None
    history_days: int = DEFAULT_HISTORY_DAYS
    google: GoogleSpec | None = None
    linkedin: LinkedInSpec | None = None
    hubspot: HubSpotSpec | None = None
    faults: tuple[Fault, ...] = ()
    platform_changes: tuple[PlatformChange, ...] = ()

    def __post_init__(self) -> None:
        """Validate the name, history and part types.

        Raises:
            ValueError: Naming the invalid part.
        """
        _check_text("WorldSpec.name", self.name)
        _check_int("history_days", self.history_days, minimum=1)
        _require(self.history_days <= 3650, "history_days must be at most 3650")
        _require(
            self.anchor is None
            or (isinstance(self.anchor, date) and not isinstance(self.anchor, datetime)),
            "anchor must be a date",
        )
        for fault in self.faults:
            _require(isinstance(fault, Fault), "faults must be Fault instances")
        for change in self.platform_changes:
            _require(isinstance(change, PlatformChange), "platform_changes must be PlatformChange")

    def with_google(self, **changes: Any) -> WorldSpec:
        """Return a copy with the Google part changed (``dataclasses.replace``).

        Raises:
            ValueError: When the spec has no Google part.
        """
        _require(self.google is not None, f"world {self.name!r} has no Google part")
        return self.with_(google=cast(GoogleSpec, self.google).with_(**changes))

    def with_linkedin(self, **changes: Any) -> WorldSpec:
        """Return a copy with the LinkedIn part changed.

        Raises:
            ValueError: When the spec has no LinkedIn part.
        """
        _require(self.linkedin is not None, f"world {self.name!r} has no LinkedIn part")
        return self.with_(linkedin=cast(LinkedInSpec, self.linkedin).with_(**changes))

    def with_hubspot(self, **changes: Any) -> WorldSpec:
        """Return a copy with the HubSpot part changed.

        Raises:
            ValueError: When the spec has no HubSpot part.
        """
        _require(self.hubspot is not None, f"world {self.name!r} has no HubSpot part")
        return self.with_(hubspot=cast(HubSpotSpec, self.hubspot).with_(**changes))

    def adding(
        self,
        *,
        faults: Iterable[Fault] = (),
        platform_changes: Iterable[PlatformChange] = (),
    ) -> WorldSpec:
        """Return a copy with ``faults`` and ``platform_changes`` appended."""
        return self.with_(
            faults=(*self.faults, *faults),
            platform_changes=(*self.platform_changes, *platform_changes),
        )


class ScenarioLike(Protocol):
    """Anything that names a world spec, such as ``scenarios.Scenario``.

    Declared here so this module never imports ``scenarios`` (which imports it).
    """

    @property
    def name(self) -> str:
        """The scenario name."""
        ...

    @property
    def spec(self) -> WorldSpec:
        """The world the scenario runs on."""
        ...


# ---------------------------------------------------------------------------
# Records returned by world reads (wire-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorldState:
    """Platform state at one instant: spec values with every platform change up to it applied.

    Attributes:
        at: The instant (UTC).
        overrides: ``(source, entity, key, field)`` to the changed value.
        absent: ``(source, entity, key)`` of entities that no listing returns at ``at``.
    """

    at: datetime
    overrides: Mapping[tuple[str, str, str, str], object]
    absent: frozenset[tuple[str, str, str]]

    def changed(self, source: str, entity: str, key: str, name: str) -> bool:
        """Whether a platform change has set this field by ``at``."""
        return (source, entity, key, name) in self.overrides

    def value(self, source: str, entity: str, key: str, name: str, default: object) -> object:
        """Return the changed value of a field, or ``default`` (the spec value)."""
        return self.overrides.get((source, entity, key, name), default)

    def present(self, source: str, entity: str, key: str) -> bool:
        """Whether listings return the entity at ``at``."""
        return (source, entity, key) not in self.absent


@dataclass(frozen=True, slots=True)
class GoogleCustomerRecord:
    """The Google customer (Q-G1)."""

    customer_id: str
    descriptive_name: str
    currency_code: str
    time_zone: str


@dataclass(frozen=True, slots=True)
class GoogleBudgetRecord:
    """A Google campaign budget at an instant.

    ``amount``/``amount_micros`` are set for ``DAILY`` budgets and ``total_amount``/
    ``total_amount_micros`` for ``CUSTOM_PERIOD`` ones (v25 leaves ``amount_micros`` unset for
    a lifetime budget). ``campaign_ids`` are the campaigns drawing from it at that instant.
    """

    id: int
    key: str
    name: str
    period: str
    explicitly_shared: bool
    amount: Decimal | None
    amount_micros: int | None
    total_amount: Decimal | None
    total_amount_micros: int | None
    campaign_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GoogleCampaignRecord:
    """A Google campaign at an instant with its budget (Q-G2, Q-G2b)."""

    id: int
    key: str
    name: str
    status: str
    channel: str
    budget_id: int
    budget_key: str
    budget_name: str
    budget_period: str
    budget_explicitly_shared: bool
    budget_amount: Decimal | None
    budget_amount_micros: int | None
    budget_total_amount: Decimal | None
    budget_total_amount_micros: int | None


@dataclass(frozen=True, slots=True)
class GoogleAdGroupRecord:
    """A Google ad group at an instant (Q-G4)."""

    id: int
    key: str
    campaign_id: int
    campaign_key: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class GoogleKeywordRecord:
    """A Google keyword criterion at an instant (Q-G7); ``quality_score`` may be unpopulated."""

    criterion_id: int
    key: str
    ad_group_id: int
    campaign_id: int
    text: str
    match_type: str
    status: str
    quality_score: int | None


@dataclass(frozen=True, slots=True)
class GoogleConversionActionRecord:
    """A Google conversion action at an instant (Q-G8), removed actions included."""

    id: int
    key: str
    name: str
    status: str
    include_in_conversions_metric: bool
    counting_type: str
    attribution_model: str


@dataclass(frozen=True, slots=True)
class GoogleCampaignDay:
    """One campaign's metrics on one Google native date (Q-G3).

    Search impression shares are ``None`` for non-search channels.
    """

    campaign_id: int
    date: date
    cost: Decimal
    cost_micros: int
    impressions: int
    clicks: int
    conversions: Decimal
    conversions_value: Decimal
    search_budget_lost_impression_share: Decimal | None
    search_impression_share: Decimal | None


@dataclass(frozen=True, slots=True)
class GoogleKeywordDay:
    """One keyword's metrics on one Google native date (Q-G5, ``keyword_view``)."""

    criterion_id: int
    ad_group_id: int
    campaign_id: int
    date: date
    cost: Decimal
    cost_micros: int
    impressions: int
    clicks: int
    conversions: Decimal
    historical_quality_score: int | None
    average_cpc: Decimal | None
    average_cpc_micros: int | None


@dataclass(frozen=True, slots=True)
class GoogleSearchTermDay:
    """One search term's metrics on one Google native date (Q-G6, ``search_term_view``).

    ``criterion_id``, ``keyword_text`` and ``match_type`` describe the matched keyword
    (``segments.keyword``); all ``None`` for traffic without a keyword criterion.
    """

    search_term: str
    status: str
    ad_group_id: int
    campaign_id: int
    criterion_id: int | None
    keyword_text: str | None
    match_type: str | None
    date: date
    cost: Decimal
    cost_micros: int
    impressions: int
    clicks: int
    conversions: Decimal
    conversions_value: Decimal


@dataclass(frozen=True, slots=True)
class LinkedInAccountRecord:
    """A LinkedIn ad account."""

    id: int
    name: str
    currency: str
    status: str
    type: str
    created_at: datetime

    @property
    def urn(self) -> str:
        """``urn:li:sponsoredAccount:{id}``."""
        return f"urn:li:sponsoredAccount:{self.id}"


@dataclass(frozen=True, slots=True)
class LinkedInCampaignGroupRecord:
    """A LinkedIn campaign group at an instant."""

    id: int
    key: str
    account_id: int
    name: str
    status: str
    daily_budget: Decimal | None
    total_budget: Decimal | None
    currency: str
    run_start: datetime
    run_end: datetime | None
    created_at: datetime
    last_modified_at: datetime

    @property
    def urn(self) -> str:
        """``urn:li:sponsoredCampaignGroup:{id}``."""
        return f"urn:li:sponsoredCampaignGroup:{self.id}"


@dataclass(frozen=True, slots=True)
class LinkedInCampaignRecord:
    """A LinkedIn campaign at an instant."""

    id: int
    key: str
    account_id: int
    group_id: int
    name: str
    status: str
    objective: str
    daily_budget: Decimal | None
    total_budget: Decimal | None
    currency: str
    run_start: datetime
    created_at: datetime
    last_modified_at: datetime
    version_tag: str

    @property
    def urn(self) -> str:
        """``urn:li:sponsoredCampaign:{id}``."""
        return f"urn:li:sponsoredCampaign:{self.id}"


@dataclass(frozen=True, slots=True)
class LinkedInCreativeRecord:
    """A LinkedIn creative at an instant; ``lead_form_id`` is ``None`` for landing pages."""

    id: int
    key: str
    account_id: int
    campaign_id: int
    intended_status: str
    post_id: int
    headline: str
    intro_text: str
    cta_label: str
    lead_form_id: int | None
    landing_page_url: str | None
    created_at: datetime
    last_modified_at: datetime

    @property
    def urn(self) -> str:
        """``urn:li:sponsoredCreative:{id}``."""
        return f"{CREATIVE_URN_PREFIX}{self.id}"

    @property
    def post_urn(self) -> str:
        """``urn:li:share:{post_id}``, the post the creative references."""
        return f"urn:li:share:{self.post_id}"

    @property
    def lead_form_urn(self) -> str | None:
        """Canonical ``urn:li:leadGenForm:{id}`` of the call-to-action, or ``None``."""
        return None if self.lead_form_id is None else f"{FORM_URN_PREFIX}{self.lead_form_id}"


@dataclass(frozen=True, slots=True)
class LinkedInPostRecord:
    """A post referenced by a creative, carrying its headline and intro text."""

    id: int
    account_id: int
    headline: str
    commentary: str
    landing_page_url: str | None
    created_at: datetime

    @property
    def urn(self) -> str:
        """``urn:li:share:{id}``."""
        return f"urn:li:share:{self.id}"


@dataclass(frozen=True, slots=True)
class LinkedInCreativeDay:
    """One creative's analytics on one UTC date (``pivot=CREATIVE``, ``DAILY``)."""

    creative_id: int
    campaign_id: int
    account_id: int
    date: date
    cost: Decimal
    impressions: int
    clicks: int
    landing_page_clicks: int
    form_opens: int
    leads: int
    website_conversions: int


@dataclass(frozen=True, slots=True)
class LinkedInSegmentRecord:
    """One campaign and demographic value over one exact requested range (``ALL``).

    Values are approximated (impressions to the nearest 10) and privacy-thresholded, so they
    are never summed from daily rows. ``None`` marks a metric the pivot does not return.
    """

    account_id: int
    campaign_id: int
    pivot: str
    value: str
    impressions: int | None
    clicks: int | None
    cost: Decimal | None
    leads: int | None
    website_conversions: int | None


@dataclass(frozen=True, slots=True)
class LinkedInQuestionRecord:
    """One question of a lead form version."""

    question_id: int
    name: str
    predefined_field: str | None
    text: str


@dataclass(frozen=True, slots=True)
class LinkedInFormVersionRecord:
    """One version of a lead form, with the instant from which responses use it."""

    form_id: int
    version: int
    questions: tuple[LinkedInQuestionRecord, ...]
    active_from: datetime

    @property
    def email_question_ids(self) -> tuple[int, ...]:
        """Ids of the ``EMAIL`` and ``WORK_EMAIL`` questions, in display order."""
        return tuple(
            question.question_id
            for question in self.questions
            if question.predefined_field in ("EMAIL", "WORK_EMAIL")
        )


@dataclass(frozen=True, slots=True)
class LinkedInLeadFormRecord:
    """A lead form at an instant, at its current version."""

    id: int
    account_id: int
    name: str
    state: str
    current_version: int
    questions: tuple[LinkedInQuestionRecord, ...]
    created_at: datetime
    last_modified_at: datetime

    @property
    def urn(self) -> str:
        """Canonical ``urn:li:leadGenForm:{id}``."""
        return f"{FORM_URN_PREFIX}{self.id}"


@dataclass(frozen=True, slots=True)
class LinkedInLeadAnswer:
    """One answer of a lead response, keyed by the version's question id."""

    question_id: int
    answer: str


@dataclass(frozen=True, slots=True)
class LinkedInLeadResponse:
    """One lead form response, test leads included.

    The plaintext ``email`` belongs to a fictional ``@sim.invalid`` person and exists only in
    the world; the product hashes it at ingest and stores only the hash.

    Attributes:
        id: Plain response id such as ``1a2b3c4d-5e6f-7a8b-9c0d-112233445566-7``.
        account_id: Owning account (the form's owner).
        campaign_id: Campaign that served it.
        creative_id: Associated creative as reported, or ``None`` (unknown creative).
        source_creative_id: The creative that generated it (simulator ground truth, never
            on the wire); daily ``leads`` count responses by this creative.
        form_id: Canonical form id.
        form_version: Form version active at submission.
        submitted_at: Submission instant (the lead's touch).
        test_lead: Whether it is a test lead.
        email: The person's email.
        answers: Answers keyed by the version's question ids.
        person_index: The simulated person's index (repeat leads share it).
    """

    id: str
    account_id: int
    campaign_id: int
    creative_id: int | None
    source_creative_id: int
    form_id: int
    form_version: int
    submitted_at: datetime
    test_lead: bool
    email: str
    answers: tuple[LinkedInLeadAnswer, ...]
    person_index: int | None

    @property
    def versioned_form_urn(self) -> str:
        """``urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{id},{version})``."""
        return f"urn:li:versionedLeadGenForm:({FORM_URN_PREFIX}{self.form_id},{self.form_version})"


@dataclass(frozen=True, slots=True)
class PropertyOption:
    """One option of an enumeration property, such as a lifecycle stage."""

    label: str
    value: str
    display_order: int
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class PropertyRecord:
    """A HubSpot property definition (``GET /crm/properties/2026-09/{object}/{name}``)."""

    object_type: str
    name: str
    label: str
    type: str
    field_type: str
    options: tuple[PropertyOption, ...] = ()


@dataclass(frozen=True, slots=True)
class ContactRecord:
    """A HubSpot contact as of the world's current instant.

    Attributes:
        id: Contact id from ``HUBSPOT_CONTACT_IDS`` or ``HUBSPOT_HASHED_CONTACT_IDS``.
        email: The ``@sim.invalid`` email; ``None`` for seeded contacts answering real lead
            emails, whose plaintext the world never sees.
        email_sha256: SHA-256 of the normalised email; ``None`` when unknown to the world.
        first_name: First name.
        last_name: Last name.
        created_at: Creation instant.
        updated_at: Last modification instant.
        lifecycle_stage: Current ``lifecyclestage`` option value.
        stage_entered: Option value to the instant the contact entered it
            (``hs_v2_date_entered_{value}``); a skipped stage is absent.
        bound_dates: Property-mode MQL/QL properties to their date (midnight UTC).
        google_campaign_ref: Google campaign id of a Google-touch contact.
        google_touch_at: Google touch instant of a Google-touch contact.
    """

    id: str
    email: str | None
    email_sha256: str | None
    first_name: str
    last_name: str
    created_at: datetime
    updated_at: datetime
    lifecycle_stage: str
    stage_entered: Mapping[str, datetime]
    bound_dates: Mapping[str, date]
    google_campaign_ref: str | None
    google_touch_at: datetime | None


@dataclass(frozen=True, slots=True)
class MeetingRecord:
    """A HubSpot meeting as of the world's current instant.

    ``created_at`` (``hs_createdate``) equals the booking instant only for booking sources
    such as ``MEETINGS_PUBLIC``.
    """

    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    timestamp: datetime
    start_at: datetime
    end_at: datetime
    outcome: str
    source: str
    contact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DealRecord:
    """A HubSpot deal; ``created_at`` (``createdate``) is what the causal check compares."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    stage: str
    pipeline: str
    amount: Decimal | None
    currency: str
    contact_ids: tuple[str, ...]


type HubSpotRecord = ContactRecord | MeetingRecord | DealRecord


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """One CRM Search filter, typed (``payloads.py`` parses the request's filter groups).

    Attributes:
        property: Property name.
        operator: ``EQ``, ``NEQ``, ``LT``, ``LTE``, ``GT``, ``GTE``, ``BETWEEN``, ``IN``,
            ``HAS_PROPERTY`` or ``NOT_HAS_PROPERTY``.
        value: Comparison value: epoch milliseconds (``int``) for date and datetime
            properties, otherwise a string or ``Decimal``.
        high_value: Upper bound of ``BETWEEN`` (inclusive).
        values: Candidates of ``IN``.
    """

    property: str
    operator: str
    value: int | str | Decimal | None = None
    high_value: int | str | Decimal | None = None
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the operator and its operands.

        Raises:
            ValueError: When the operator is unknown or misses an operand.
        """
        _check_text("SearchFilter.property", self.property)
        _check_choice("SearchFilter.operator", self.operator, _SEARCH_OPERATORS)
        if self.operator in ("EQ", "NEQ", "LT", "LTE", "GT", "GTE"):
            _require(self.value is not None, f"{self.operator} needs a value")
        elif self.operator == "BETWEEN":
            _require(
                self.value is not None and self.high_value is not None,
                "BETWEEN needs value and high_value",
            )
        elif self.operator == "IN":
            _require(bool(self.values), "IN needs values")


# ---------------------------------------------------------------------------
# DEFAULT_SPEC: the end-to-end world in which every section populates
# ---------------------------------------------------------------------------

_DECIMAL_PROFILE_FIELDS: Final = frozenset(
    {
        "spend_day_share",
        "weekend_factor",
        "form_open_rate",
        "form_completion_rate",
        "trend",
        "noise",
        "value_per_conversion",
        "landing_page_click_share",
    }
)


def _dp(
    impressions: int, ctr: str, cpc: str, conversion_rate: str = "0", **extra: Any
) -> DailyProfile:
    """Build a :class:`DailyProfile` from string rates (exact ``Decimal`` values)."""
    converted: dict[str, Any] = {
        name: Decimal(value)
        if name in _DECIMAL_PROFILE_FIELDS and isinstance(value, str)
        else value
        for name, value in extra.items()
    }
    return DailyProfile(
        impressions_mean=impressions,
        ctr=Decimal(ctr),
        cpc=Decimal(cpc),
        conversion_rate=Decimal(conversion_rate),
        **converted,
    )


def _q(question_id: int, name: str, predefined: str | None, text: str) -> QuestionSpec:
    """Build a :class:`QuestionSpec`."""
    return QuestionSpec(question_id=question_id, name=name, predefined_field=predefined, text=text)


def _default_google() -> GoogleSpec:
    """The default Google Ads customer (USD, ``America/New_York``).

    Designed so that, over any 28-day window ending near the anchor:

    * the account converts about 4-5% of clicks over 90 days (K1 baseline usable, n* about 70);
    * K1 fires on "free attribution software download" (NONE) and "attribution modeling"
      (ADDED, naming its matched criterion), and on the competitor term; EXCLUDED and
      ADDED_EXCLUDED terms are skipped;
    * K2 fires on "marketing attribution software" with six Jaccard >= 0.5 neighbours (capped
      at five) and on the forecasting terms;
    * K3 fires on "multi touch attribution" (quality score 7 to 5 at offset -14) and on
      "attribution platform cost" (CPC rising 4% of base per day from offset -30, flat
      conversion rate);
    * C3b fires on the "revenue forecasting" cluster, whose head tokens appear in no keyword,
      ad group or campaign name;
    * C1 fires on "Competitor - Conquest" (spend on about 85% of days, no conversions);
    * after about a week of snapshots C2/B1 fire on "Brand - Search" (capped, efficient), B2 on
      "Retargeting - Broad" (capped, poor efficiency) and B3 on "Display - Awareness" (about
      36% utilization); the shared budget runs at about 83%;
    * search-term coverage is about 85-90% and every included conversion action is last-click
      with whole-number conversions, so the B4 Google counting contract can hold.
    """
    budgets = (
        GoogleBudgetSpec("b_brand", Decimal("200"), name="Brand - Search budget"),
        GoogleBudgetSpec("b_generic", Decimal("400"), name="Generic - Attribution budget"),
        GoogleBudgetSpec("b_competitor", Decimal("150"), name="Competitor - Conquest budget"),
        GoogleBudgetSpec("b_retargeting", Decimal("120"), name="Retargeting - Broad budget"),
        GoogleBudgetSpec("b_display", Decimal("300"), name="Display - Awareness budget"),
        GoogleBudgetSpec(
            "b_shared", Decimal("250"), explicitly_shared=True, name="Product - Shared budget"
        ),
        GoogleBudgetSpec(
            "b_webinar", Decimal("9000"), period="CUSTOM_PERIOD", name="Webinar - Lifetime budget"
        ),
        GoogleBudgetSpec("b_seasonal", Decimal("60"), name="Seasonal - Q4 promo budget"),
        GoogleBudgetSpec("b_legacy", Decimal("80"), name="Legacy - Old brand budget"),
    )
    campaigns = (
        GoogleCampaignSpec(
            "g_brand",
            "Brand - Search",
            "b_brand",
            profile=_dp(4000, "0.08", "1.20", "0.10", weekend_factor="0.5", noise="0.08"),
        ),
        GoogleCampaignSpec("g_generic", "Generic - Attribution software", "b_generic"),
        GoogleCampaignSpec(
            "g_competitor",
            "Competitor - Conquest",
            "b_competitor",
            profile=_dp(1200, "0.03", "2.50", spend_day_share="0.85"),
        ),
        GoogleCampaignSpec(
            "g_retargeting",
            "Retargeting - Broad",
            "b_retargeting",
            profile=_dp(3000, "0.03", "1.50", "0.005"),
        ),
        GoogleCampaignSpec(
            "g_display",
            "Display - Awareness",
            "b_display",
            channel="DISPLAY",
            profile=_dp(30000, "0.004", "0.90", "0.01"),
        ),
        GoogleCampaignSpec(
            "g_product_analytics",
            "Product - Analytics platform",
            "b_shared",
            profile=_dp(2000, "0.03", "2.00", "0.06"),
        ),
        GoogleCampaignSpec(
            "g_product_reporting",
            "Product - Reporting tool",
            "b_shared",
            profile=_dp(1600, "0.025", "2.20", "0.05"),
        ),
        GoogleCampaignSpec(
            "g_webinar",
            "Launch - Webinar series",
            "b_webinar",
            profile=_dp(1000, "0.03", "1.50", "0.05"),
        ),
        GoogleCampaignSpec(
            "g_seasonal",
            "Seasonal - Q4 promo",
            "b_seasonal",
            status="PAUSED",
            profile=_dp(800, "0.03", "2.00", "0.04", active_until_offset=-45),
        ),
        GoogleCampaignSpec(
            "g_legacy",
            "Legacy - Old brand",
            "b_legacy",
            status="REMOVED",
            profile=_dp(900, "0.03", "1.80", "0.03", active_until_offset=-150),
        ),
    )
    ad_groups = (
        GoogleAdGroupSpec("ag_brand", "g_brand", "Brand terms"),
        GoogleAdGroupSpec("ag_core", "g_generic", "Attribution software"),
        GoogleAdGroupSpec("ag_analytics", "g_generic", "Analytics software"),
        GoogleAdGroupSpec("ag_competitor", "g_competitor", "Competitors"),
        GoogleAdGroupSpec("ag_retargeting", "g_retargeting", "Past visitors"),
        GoogleAdGroupSpec("ag_display", "g_display", "In-market audiences"),
        GoogleAdGroupSpec("ag_product_analytics", "g_product_analytics", "Analytics platform"),
        GoogleAdGroupSpec("ag_product_reporting", "g_product_reporting", "Reporting tool"),
        GoogleAdGroupSpec("ag_webinar", "g_webinar", "Webinar registrations"),
        GoogleAdGroupSpec("ag_seasonal", "g_seasonal", "Holiday offers"),
        GoogleAdGroupSpec("ag_legacy", "g_legacy", "Old brand terms"),
    )
    keywords = (
        GoogleKeywordSpec(
            "kw_brand",
            "ag_brand",
            "acme analytics",
            match_type="EXACT",
            quality_score=9,
            profile=_dp(60, "0.10", "1.20", "0.10", weekend_factor="0.5"),
        ),
        GoogleKeywordSpec(
            "kw_attr_sw",
            "ag_core",
            "attribution software",
            quality_score=7,
            profile=_dp(40, "0.05", "3.00", "0.03"),
        ),
        GoogleKeywordSpec(
            "kw_attr_model",
            "ag_core",
            "attribution modeling",
            match_type="EXACT",
            quality_score=6,
            profile=_dp(10, "0.05", "3.00"),
        ),
        GoogleKeywordSpec(
            "kw_mta",
            "ag_core",
            "multi touch attribution",
            quality_score=5,
            qs_profile=QualityScoreProfile(7, change_offset=-14, changed_score=5),
            profile=_dp(15, "0.04", "3.10", "0.04"),
        ),
        GoogleKeywordSpec(
            "kw_platform_cost",
            "ag_core",
            "attribution platform cost",
            match_type="EXACT",
            quality_score=6,
            profile=_dp(40, "0.05", "3.00", "0.05", trend="0.04", trend_from_offset=-30),
        ),
        GoogleKeywordSpec(
            "kw_attr_reporting",
            "ag_core",
            "attribution reporting",
            quality_score=None,
        ),
        GoogleKeywordSpec(
            "kw_sales_analytics",
            "ag_analytics",
            "sales analytics software",
            match_type="BROAD",
            quality_score=6,
            profile=_dp(20, "0.05", "2.50", "0.04"),
        ),
        GoogleKeywordSpec(
            "kw_rival",
            "ag_competitor",
            "rival attribution suite",
            match_type="BROAD",
            quality_score=5,
            profile=_dp(20, "0.03", "2.50", spend_day_share="0.85"),
        ),
        GoogleKeywordSpec(
            "kw_product_analytics", "ag_product_analytics", "analytics platform", quality_score=7
        ),
        GoogleKeywordSpec(
            "kw_product_reporting", "ag_product_reporting", "reporting tool", quality_score=6
        ),
    )
    neighbour = _dp(25, "0.04", "3.00", "0.05")
    forecasting = _dp(75, "0.04", "2.50", "0.08")
    rising = _dp(340, "0.05", "3.00", "0.05", trend="0.04", trend_from_offset=-30)
    search_terms = (
        GoogleSearchTermSpec(
            "acme analytics",
            "ag_brand",
            "kw_brand",
            status="ADDED",
            profile=_dp(300, "0.10", "1.20", "0.10", weekend_factor="0.5"),
        ),
        GoogleSearchTermSpec(
            "marketing attribution software",
            "ag_core",
            "kw_attr_sw",
            profile=_dp(80, "0.05", "3.00", "0.12"),
        ),
        GoogleSearchTermSpec(
            "marketing attribution tool", "ag_core", "kw_attr_sw", profile=neighbour
        ),
        GoogleSearchTermSpec(
            "marketing attribution platform", "ag_core", "kw_attr_sw", profile=neighbour
        ),
        GoogleSearchTermSpec(
            "b2b marketing attribution", "ag_core", "kw_attr_sw", profile=neighbour
        ),
        GoogleSearchTermSpec(
            "marketing attribution software pricing", "ag_core", "kw_attr_sw", profile=neighbour
        ),
        GoogleSearchTermSpec(
            "marketing attribution software free", "ag_core", "kw_attr_sw", profile=neighbour
        ),
        GoogleSearchTermSpec(
            "attribution software comparison", "ag_core", "kw_attr_sw", profile=neighbour
        ),
        GoogleSearchTermSpec(
            "free attribution software download",
            "ag_core",
            "kw_attr_sw",
            profile=_dp(150, "0.04", "3.00"),
        ),
        GoogleSearchTermSpec(
            "attribution software jobs",
            "ag_core",
            "kw_attr_sw",
            status="EXCLUDED",
            profile=_dp(50, "0.04", "3.00"),
        ),
        GoogleSearchTermSpec(
            "attribution software salary",
            "ag_core",
            "kw_attr_sw",
            status="ADDED_EXCLUDED",
            profile=_dp(40, "0.04", "3.00"),
        ),
        GoogleSearchTermSpec(
            "attribution modeling",
            "ag_core",
            "kw_attr_model",
            status="ADDED",
            profile=_dp(100, "0.04", "3.00"),
        ),
        GoogleSearchTermSpec(
            "multi touch attribution",
            "ag_core",
            "kw_mta",
            status="ADDED",
            profile=_dp(110, "0.04", "3.10", "0.05"),
        ),
        GoogleSearchTermSpec(
            "attribution platform cost",
            "ag_core",
            "kw_platform_cost",
            status="ADDED",
            profile=rising,
        ),
        GoogleSearchTermSpec(
            "revenue forecasting tool", "ag_analytics", "kw_sales_analytics", profile=forecasting
        ),
        GoogleSearchTermSpec(
            "revenue forecasting software",
            "ag_analytics",
            "kw_sales_analytics",
            profile=forecasting,
        ),
        GoogleSearchTermSpec(
            "b2b revenue forecasting", "ag_analytics", "kw_sales_analytics", profile=forecasting
        ),
        GoogleSearchTermSpec(
            "rival attribution suite alternative",
            "ag_competitor",
            "kw_rival",
            profile=_dp(130, "0.03", "2.50", spend_day_share="0.85"),
        ),
    )
    conversion_actions = (
        GoogleConversionActionSpec("ca_demo", "Demo request"),
        GoogleConversionActionSpec(
            "ca_newsletter",
            "Newsletter signup",
            include_in_conversions_metric=False,
            counting_type="MANY_PER_CLICK",
            attribution_model="GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
        ),
        GoogleConversionActionSpec("ca_legacy", "Legacy form", status="REMOVED"),
    )
    return GoogleSpec(
        name="Simulated Ads - Google",
        budgets=budgets,
        campaigns=campaigns,
        ad_groups=ad_groups,
        keywords=keywords,
        search_terms=search_terms,
        conversion_actions=conversion_actions,
    )


def _default_linkedin() -> LinkedInSpec:
    """The default LinkedIn advertiser: a USD and a EUR account (two currency cohorts).

    The lead forms are exactly the forms of the shipped ``config/content_assets.yaml``
    (990000101 with two versions whose email question id differs, 990000102, 990000201,
    990000301) and the landing-page creative 990001301 is the registry's override for
    ``attribution-playbook``; :class:`SimulatedWorld` checks this at construction. Designed so
    that:

    * every lead-gen campaign has two variants above 1,000 impressions per window; the
      pipeline report's "question headline" variant has a weak open rate (CR2/CR3) and its pair
      is distinguishable after Holm correction (CR1); the EMEA pair is not distinguishable;
    * "Benchmarks - Lead gen (NA)" converts far worse on weekends (B4 fires on first start) and
      at about a third of the others' cost per lead (W1 fires on ``paid-media-benchmarks-2026``);
    * "Pipeline Report - Lead gen (NA)" reaches Directors (``urn:li:seniority:6``) on 10% of
      impressions at half the campaign's cost per lead (C3a), and its industry pivot returns
      no lead metric;
    * campaigns span ACTIVE, PAUSED, ARCHIVED and COMPLETED, groups ACTIVE, PAUSED, ARCHIVED,
      DRAFT and CANCELED; 2% of responses are test leads and 2% carry no creative.
    """
    na, emea = 990_000_001, 990_000_002
    accounts = (
        LinkedInAccountSpec(na, "Simulated Ads - North America", "USD"),
        LinkedInAccountSpec(emea, "Simulated Ads - EMEA", "EUR"),
    )
    groups = (
        LinkedInCampaignGroupSpec("grp_na_leadgen", na, "NA - Always-on lead gen"),
        LinkedInCampaignGroupSpec(
            "grp_na_webinars", na, "NA - Webinars 2026", status="ARCHIVED", run_end_offset=-90
        ),
        LinkedInCampaignGroupSpec("grp_na_brand", na, "NA - Brand", status="PAUSED"),
        LinkedInCampaignGroupSpec(
            "grp_na_planning", na, "NA - Q1 2027 planning", status="DRAFT", run_start_offset=60
        ),
        LinkedInCampaignGroupSpec("grp_emea_leadgen", emea, "EMEA - Lead gen"),
        LinkedInCampaignGroupSpec("grp_emea_tests", emea, "EMEA - Tests", status="CANCELED"),
    )
    campaigns = (
        LinkedInCampaignSpec(
            "li_pipeline_na",
            na,
            "grp_na_leadgen",
            "Pipeline Report - Lead gen (NA)",
            daily_budget=Decimal("250.00"),
        ),
        LinkedInCampaignSpec(
            "li_benchmarks_na",
            na,
            "grp_na_leadgen",
            "Benchmarks - Lead gen (NA)",
            daily_budget=Decimal("220.00"),
        ),
        LinkedInCampaignSpec(
            "li_playbook_na",
            na,
            "grp_na_leadgen",
            "Attribution Playbook - Lead gen",
            daily_budget=Decimal("150.00"),
        ),
        LinkedInCampaignSpec(
            "li_playbook_site",
            na,
            "grp_na_leadgen",
            "Attribution Playbook - Website visits",
            objective="WEBSITE_CONVERSION",
            daily_budget=Decimal("90.00"),
        ),
        LinkedInCampaignSpec(
            "li_webinar_q2",
            na,
            "grp_na_webinars",
            "Webinar series - Q2",
            status="COMPLETED",
            total_budget=Decimal("6000.00"),
        ),
        LinkedInCampaignSpec(
            "li_brand_2025",
            na,
            "grp_na_brand",
            "Brand awareness - 2025",
            status="ARCHIVED",
            objective="BRAND_AWARENESS",
            daily_budget=Decimal("50.00"),
        ),
        LinkedInCampaignSpec(
            "li_pipeline_emea",
            emea,
            "grp_emea_leadgen",
            "Pipeline Report - Lead gen (EMEA)",
            daily_budget=Decimal("140.00"),
        ),
        LinkedInCampaignSpec(
            "li_retarget_emea",
            emea,
            "grp_emea_leadgen",
            "EMEA - Site retargeting",
            status="PAUSED",
            objective="WEBSITE_VISIT",
            daily_budget=Decimal("60.00"),
        ),
    )
    creatives = (
        LinkedInCreativeSpec(
            "cr_pipeline_stat",
            "li_pipeline_na",
            "73% of B2B pipeline starts before the first sales call",
            "Get the 2026 B2B Pipeline Report: benchmarks from 1,200 revenue teams.",
            lead_form_id=990_000_101,
            profile=_dp(
                3000,
                "0.009",
                "3.20",
                form_open_rate="0.006",
                form_completion_rate="0.11",
                noise="0.08",
            ),
        ),
        LinkedInCreativeSpec(
            "cr_pipeline_question",
            "li_pipeline_na",
            "Where does your pipeline really come from?",
            "Get the 2026 B2B Pipeline Report: benchmarks from 1,200 revenue teams.",
            lead_form_id=990_000_101,
            profile=_dp(
                3000,
                "0.006",
                "3.20",
                form_open_rate="0.0022",
                form_completion_rate="0.11",
                noise="0.08",
            ),
        ),
        LinkedInCreativeSpec(
            "cr_benchmarks_cpl",
            "li_benchmarks_na",
            "What does a good cost per lead look like in 2026?",
            "Paid Media Benchmarks 2026: CPL, CTR and CPC by channel and industry.",
            lead_form_id=990_000_201,
            profile=_dp(
                3500,
                "0.010",
                "1.60",
                form_open_rate="0.008",
                form_completion_rate="0.20",
                weekend_factor="0.15",
            ),
        ),
        LinkedInCreativeSpec(
            "cr_benchmarks_ctr",
            "li_benchmarks_na",
            "Your CTR versus 4,000 B2B advertisers",
            "Paid Media Benchmarks 2026: CPL, CTR and CPC by channel and industry.",
            lead_form_id=990_000_201,
            profile=_dp(
                2500,
                "0.009",
                "1.60",
                form_open_rate="0.007",
                form_completion_rate="0.16",
                weekend_factor="0.15",
            ),
        ),
        LinkedInCreativeSpec(
            "cr_playbook_guide",
            "li_playbook_na",
            "The Attribution Playbook",
            "A practical guide to multi-touch attribution for B2B marketers.",
            lead_form_id=990_000_301,
            profile=_dp(2500, "0.007", "3.60", form_open_rate="0.005", form_completion_rate="0.10"),
        ),
        LinkedInCreativeSpec(
            "cr_playbook_checklist",
            "li_playbook_na",
            "Stop guessing which channel works",
            "A practical guide to multi-touch attribution for B2B marketers.",
            cta_label="LEARN_MORE",
            lead_form_id=990_000_301,
            profile=_dp(
                2000, "0.007", "3.60", form_open_rate="0.0045", form_completion_rate="0.06"
            ),
        ),
        LinkedInCreativeSpec(
            "cr_playbook_landing",
            "li_playbook_site",
            "Read the Attribution Playbook online",
            "The full playbook, free on our site.",
            id=990_001_301,
            cta_label="LEARN_MORE",
            landing_page_url="https://www.sim.invalid/attribution-playbook",
            profile=_dp(4000, "0.006", "2.50", "0.04"),
        ),
        LinkedInCreativeSpec(
            "cr_webinar_q2",
            "li_webinar_q2",
            "Live: attribution that sales trusts",
            "Join our Q2 webinar series.",
            cta_label="REGISTER",
            lead_form_id=990_000_301,
            profile=_dp(
                2000,
                "0.006",
                "3.00",
                form_open_rate="0.004",
                form_completion_rate="0.10",
                active_from_offset=-200,
                active_until_offset=-90,
            ),
        ),
        LinkedInCreativeSpec(
            "cr_brand_2025",
            "li_brand_2025",
            "Meet the attribution platform for revenue teams",
            "See how revenue teams prove pipeline.",
            cta_label="LEARN_MORE",
            landing_page_url="https://www.sim.invalid/",
            profile=_dp(
                6000, "0.004", "1.80", "0.01", active_from_offset=-210, active_until_offset=-150
            ),
        ),
        LinkedInCreativeSpec(
            "cr_pipeline_emea_stat",
            "li_pipeline_emea",
            "Seven in ten EMEA deals start with content",
            "The 2026 B2B Pipeline Report, EMEA edition.",
            lead_form_id=990_000_102,
            profile=_dp(2200, "0.008", "3.00", form_open_rate="0.006", form_completion_rate="0.12"),
        ),
        LinkedInCreativeSpec(
            "cr_pipeline_emea_question",
            "li_pipeline_emea",
            "Is your EMEA pipeline on track?",
            "The 2026 B2B Pipeline Report, EMEA edition.",
            lead_form_id=990_000_102,
            profile=_dp(
                2000, "0.008", "3.00", form_open_rate="0.0058", form_completion_rate="0.12"
            ),
        ),
        LinkedInCreativeSpec(
            "cr_retarget_emea",
            "li_retarget_emea",
            "Come back to the attribution platform",
            "Pick up where you left off.",
            cta_label="LEARN_MORE",
            landing_page_url="https://www.sim.invalid/emea",
            profile=_dp(3000, "0.005", "2.00", "0.02", active_until_offset=-30),
        ),
    )
    email_v1 = (
        _q(1001, "firstName", "FIRST_NAME", "First name"),
        _q(1002, "lastName", "LAST_NAME", "Last name"),
        _q(1003, "email", "EMAIL", "Email address"),
        _q(1004, "company", "COMPANY_NAME", "Company name"),
    )
    # Version 2 reuses question id 1003 for the job title and asks the work email as 1005.
    email_v2 = (
        _q(1001, "firstName", "FIRST_NAME", "First name"),
        _q(1002, "lastName", "LAST_NAME", "Last name"),
        _q(1003, "jobTitle", "JOB_TITLE", "Job title"),
        _q(1005, "workEmail", "WORK_EMAIL", "Work email"),
        _q(1004, "company", "COMPANY_NAME", "Company name"),
    )
    lead_forms = (
        LinkedInLeadFormSpec(
            990_000_101,
            na,
            "2026 B2B Pipeline Report - Download",
            (
                FormVersionSpec(1, email_v1),
                FormVersionSpec(2, email_v2, active_from_offset=-60),
            ),
        ),
        LinkedInLeadFormSpec(
            990_000_102,
            emea,
            "2026 B2B Pipeline Report - EMEA",
            (
                FormVersionSpec(
                    1,
                    (
                        _q(2001, "firstName", "FIRST_NAME", "First name"),
                        _q(2002, "lastName", "LAST_NAME", "Last name"),
                        _q(2003, "workEmail", "WORK_EMAIL", "Work email"),
                        _q(2004, "country", "COUNTRY", "Country"),
                    ),
                ),
            ),
        ),
        LinkedInLeadFormSpec(
            990_000_201,
            na,
            "Paid Media Benchmarks 2026",
            (
                FormVersionSpec(
                    1,
                    (
                        _q(3001, "firstName", "FIRST_NAME", "First name"),
                        _q(3002, "lastName", "LAST_NAME", "Last name"),
                        _q(3003, "workEmail", "WORK_EMAIL", "Work email"),
                        _q(3004, "jobTitle", "JOB_TITLE", "Job title"),
                    ),
                ),
            ),
        ),
        LinkedInLeadFormSpec(
            990_000_301,
            na,
            "The Attribution Playbook",
            (
                FormVersionSpec(
                    1,
                    (
                        _q(4001, "firstName", "FIRST_NAME", "First name"),
                        _q(4002, "lastName", "LAST_NAME", "Last name"),
                        _q(4003, "email", "EMAIL", "Email address"),
                        _q(4004, "company", "COMPANY_NAME", "Company name"),
                        _q(4005, "challenge", None, "What is your biggest attribution challenge?"),
                    ),
                ),
            ),
        ),
    )
    segments = (
        LinkedInSegmentSpec(
            "li_pipeline_na",
            "MEMBER_SENIORITY",
            (SegmentValueSpec("urn:li:seniority:6", Decimal("0.10"), Decimal("0.5")),),
        ),
        LinkedInSegmentSpec("li_pipeline_na", "MEMBER_INDUSTRY", leads_available=False),
    )
    return LinkedInSpec(
        accounts=accounts,
        campaign_groups=groups,
        campaigns=campaigns,
        creatives=creatives,
        lead_forms=lead_forms,
        segments=segments,
        test_lead_share=Decimal("0.02"),
        unknown_creative_share=Decimal("0.02"),
    )


def _default_spec() -> WorldSpec:
    """Build :data:`DEFAULT_SPEC`."""
    return WorldSpec(
        name="default",
        google=_default_google(),
        linkedin=_default_linkedin(),
        hubspot=HubSpotSpec(orphan_deals=6),
    )


DEFAULT_SPEC: Final[WorldSpec] = _default_spec()
"""The end-to-end world: every section populates on first start (AAP 0.11.2).

Keywords (K1, K2, K3), Campaigns (C1, C3a, C3b), Budget (B4 plus withheld B1-B3 lines until
snapshot history exists; then C2/B1, B2 and B3), Copy (CR1-CR3) and White papers (W rows with
W1 on one asset; MQL bound to the custom "First meeting booked" stage, QL to the custom
"Qualified Lead" stage). The platform state is static, so the first entity sync's baselines
match it and no discrepancy arises until a user decision differs from it. No Google CRM
properties are configured, so Google items carry ``no_crm_link``.
"""


# ---------------------------------------------------------------------------
# The shared core: time, platform state and deterministic draws
# ---------------------------------------------------------------------------

type _ProfileKind = Literal["google", "lead_gen", "landing"]

_STATE_CACHE_LIMIT: Final = 4096


def _bounded(profile: Profile | None) -> bool:
    """Whether a profile encodes when it stopped delivering (explicit days or an end offset)."""
    if isinstance(profile, ExplicitDays):
        return True
    return isinstance(profile, DailyProfile) and profile.active_until_offset is not None


def _partial(metrics: DayMetrics, fraction: Decimal) -> DayMetrics:
    """Scale a full day to the elapsed ``fraction`` of the current native day.

    Every value is floored, so partial children never sum above their partial parent.
    """
    if fraction >= 1:
        return metrics
    if fraction <= 0 or metrics.is_zero:
        return _ZERO_DAY

    def count(value: int) -> int:
        return int((value * fraction).to_integral_value(ROUND_FLOOR))

    conversions = metrics.conversions * fraction
    if metrics.conversions == metrics.conversions.to_integral_value():
        conversions = conversions.to_integral_value(ROUND_FLOOR)
    else:
        conversions = conversions.quantize(_MICRO, ROUND_FLOOR)
    impressions = count(metrics.impressions)
    clicks = min(impressions, count(metrics.clicks))
    form_opens = min(clicks, count(metrics.form_opens))
    return DayMetrics(
        cost=(metrics.cost * fraction).quantize(_CENT, ROUND_FLOOR),
        impressions=impressions,
        clicks=clicks,
        conversions=conversions,
        conversions_value=(metrics.conversions_value * fraction).quantize(_CENT, ROUND_FLOOR),
        form_opens=form_opens,
        leads=min(form_opens, count(metrics.leads)),
        landing_page_clicks=min(clicks, count(metrics.landing_page_clicks)),
        website_conversions=count(metrics.website_conversions),
        budget_lost_is=metrics.budget_lost_is,
        impression_share=metrics.impression_share,
        historical_quality_score=metrics.historical_quality_score,
        average_cpc=metrics.average_cpc,
    )


class _Core:
    """State shared by every part of one world: seed, anchor, clock, platform state, draws.

    The only mutable state is caches (draw phases, states per instant), each guarded by a lock;
    every cached value is a pure function of its key, so a race only recomputes it.
    """

    def __init__(self, seed: int, spec: WorldSpec, clock: Callable[[], datetime]) -> None:
        self.seed = seed
        self.spec = spec
        self._clock = clock
        self.anchor: date = spec.anchor if spec.anchor is not None else self.now().date()
        self.history_start: date = self.anchor - timedelta(days=spec.history_days)
        self.history_start_offset = -spec.history_days
        self._lock = threading.Lock()
        self._phases: dict[tuple[str, str], float] = {}
        ordered = sorted(
            enumerate(spec.platform_changes),
            key=lambda item: (item[1].instant(self.anchor), item[0]),
        )
        self.changes: tuple[PlatformChange, ...] = tuple(change for _index, change in ordered)
        self._instants = [change.instant(self.anchor) for change in self.changes]
        self._reappear = [change.reappear_instant(self.anchor) for change in self.changes]
        prefixes: list[Mapping[tuple[str, str, str, str], object]] = [MappingProxyType({})]
        running: dict[tuple[str, str, str, str], object] = {}
        for change in self.changes:
            if not change.disappear and change.field is not None:
                running[(change.source, change.entity, change.key, change.field)] = change.value
            prefixes.append(MappingProxyType(dict(running)))
        self._prefixes = prefixes
        self._states: dict[datetime, WorldState] = {}

    # -- time ---------------------------------------------------------------------------

    def now(self) -> datetime:
        """Return the clock's instant in UTC.

        Raises:
            ValueError: When the clock returns a naive datetime.
        """
        return _require_aware("clock()", self._clock())

    def offset(self, day: date) -> int:
        """Return the day offset of ``day`` from the anchor."""
        return (day - self.anchor).days

    def day_bounds(self, day: date, tz: ZoneInfo | None) -> tuple[datetime, datetime]:
        """Return the UTC instants at which native ``day`` starts and the next day starts."""
        return _midnight(day, tz), _midnight(day + _DAY, tz)

    def today(self, tz: ZoneInfo | None) -> date:
        """Return the current native date in ``tz`` (UTC when ``None``)."""
        now = self.now()
        return now.date() if tz is None else now.astimezone(tz).date()

    def fraction(self, day: date, tz: ZoneInfo | None) -> Decimal:
        """Return the elapsed share of native ``day``: 1 for past days, 0 for future ones."""
        now = self.now()
        start, end = self.day_bounds(day, tz)
        if now >= end:
            return Decimal(1)
        if now <= start:
            return Decimal(0)
        elapsed = (now - start) // timedelta(microseconds=1)
        total = (end - start) // timedelta(microseconds=1)
        return Decimal(elapsed) / Decimal(total)

    def days(self, start: date, end: date, tz: ZoneInfo | None) -> list[date]:
        """Return the native dates of ``[start, end]`` that exist: in history and not future.

        Raises:
            ValueError: When ``start`` is after ``end``.
        """
        _require(start <= end, f"start {start} is after end {end}")
        first = max(start, self.history_start)
        last = min(end, self.today(tz))
        if first > last:
            return []
        return [first + timedelta(days=step) for step in range((last - first).days + 1)]

    # -- platform state -------------------------------------------------------------------

    def state_at(self, instant: datetime) -> WorldState:
        """Return the platform state at ``instant`` (every change at or before it applied)."""
        at = _require_aware("state_at instant", instant)
        cached = self._states.get(at)
        if cached is not None:
            return cached
        applied = bisect.bisect_right(self._instants, at)
        absent: set[tuple[str, str, str]] = set()
        for index in range(applied):
            change = self.changes[index]
            if change.disappear:
                identity = (change.source, change.entity, change.key)
                reappear = self._reappear[index]
                if reappear is None or reappear > at:
                    absent.add(identity)
                else:
                    absent.discard(identity)
        state = WorldState(at=at, overrides=self._prefixes[applied], absent=frozenset(absent))
        with self._lock:
            if len(self._states) >= _STATE_CACHE_LIMIT:
                self._states.clear()
            self._states[at] = state
        return state

    def applied_changes(
        self, source: str, entity: str, key: str, at: datetime
    ) -> tuple[PlatformChange, ...]:
        """Return the changes to one entity at or before ``at``, in instant order."""
        applied = bisect.bisect_right(self._instants, at)
        return tuple(
            change
            for change in self.changes[:applied]
            if (change.source, change.entity, change.key) == (source, entity, key)
        )

    def status_allows(
        self,
        source: str,
        entity: str,
        key: str,
        initial: str,
        serving: tuple[str, ...],
        *,
        bounded: bool,
        day: date,
        tz: ZoneInfo | None,
    ) -> bool:
        """Whether an entity's status lets it deliver on native ``day``.

        A day delivers when the entity is listed during it and its status is serving at the
        day's start or end (a mid-day pause still served the morning). Without a status change,
        the spec status rules; a non-serving spec status still delivers through a ``bounded``
        profile, which encodes when the entity stopped (it was serving until then).
        """
        start, end = self.day_bounds(day, tz)
        last = end - timedelta(microseconds=1)
        at_start = self.state_at(start)
        at_end = self.state_at(last)
        if not at_start.present(source, entity, key) and not at_end.present(source, entity, key):
            return False
        if at_end.changed(source, entity, key, "status"):
            end_status = at_end.value(source, entity, key, "status", initial)
            start_status = at_start.value(source, entity, key, "status", initial)
            return end_status in serving or start_status in serving
        return initial in serving or bounded

    # -- deterministic draws ----------------------------------------------------------------

    def weyl(self, key: str, metric: str, ordinal: int) -> float:
        """Return the golden-ratio rounding threshold in ``[0, 1)`` of one metric on one day."""
        phase = self._phases.get((key, metric))
        if phase is None:
            phase = _rng(self.seed, "phase", f"{key}:{metric}").random()
            with self._lock:
                self._phases[(key, metric)] = phase
        return (phase + ordinal * _GOLDEN) % 1.0

    def day_rng(self, namespace: str, key: str, day: date) -> random.Random:
        """Return the generator of one entity on one native date."""
        return _rng(self.seed, namespace, f"{key}:{day.isoformat()}")

    def profile_day(
        self,
        key: str,
        profile: DailyProfile,
        day: date,
        *,
        scale: float,
        kind: _ProfileKind,
    ) -> DayMetrics:
        """Evaluate a :class:`DailyProfile` on one native day (a full day).

        Args:
            key: The entity key, namespaced by source and entity type.
            profile: The profile.
            day: The native date.
            scale: Delivery multiplier below 1 when a budget caps the demand.
            kind: ``google`` (conversions per click), ``lead_gen`` (form opens and leads) or
                ``landing`` (landing-page clicks and website conversions).

        Returns:
            The day's metrics; zero when the profile does not deliver that day.
        """
        offset = self.offset(day)
        if not profile.is_active(offset) or scale <= 0.0:
            return _ZERO_DAY
        ordinal = day.toordinal()
        share = float(profile.spend_day_share)
        if share < 1.0 and self.weyl(key, "spend", ordinal) >= share:
            return _ZERO_DAY
        rng = self.day_rng("day", key, day)
        noise = float(profile.noise)
        impression_jitter = 1.0 + noise * (2.0 * rng.random() - 1.0)
        cpc_jitter = 1.0 + noise / 2.0 * (2.0 * rng.random() - 1.0)
        value_jitter = 1.0 + noise * (2.0 * rng.random() - 1.0)
        fraction_jitter = 0.8 + 0.4 * rng.random()
        impressions = _count(
            profile.impressions_mean * impression_jitter * scale,
            self.weyl(key, "impressions", ordinal),
        )
        if impressions == 0:
            return _ZERO_DAY
        clicks = min(
            impressions, _count(impressions * float(profile.ctr), self.weyl(key, "clicks", ordinal))
        )
        cpc = float(profile.cpc) * profile.trend_factor(offset, self.history_start_offset)
        cost = _money(clicks * cpc * cpc_jitter)
        outcome_factor = float(profile.weekend_factor) if day.weekday() >= 5 else 1.0
        conversions = Decimal(0)
        form_opens = leads = landing_page_clicks = website_conversions = 0
        if kind == "lead_gen":
            form_opens = min(
                clicks,
                _count(
                    impressions * float(profile.form_open_rate), self.weyl(key, "opens", ordinal)
                ),
            )
            leads = min(
                form_opens,
                _count(
                    form_opens * float(profile.form_completion_rate) * outcome_factor,
                    self.weyl(key, "leads", ordinal),
                ),
            )
        elif kind == "landing":
            landing_page_clicks = min(
                clicks,
                _count(
                    clicks * float(profile.landing_page_click_share),
                    self.weyl(key, "landing", ordinal),
                ),
            )
            website_conversions = _count(
                landing_page_clicks * float(profile.conversion_rate) * outcome_factor,
                self.weyl(key, "website", ordinal),
            )
        else:
            expected = clicks * float(profile.conversion_rate) * outcome_factor
            if profile.fractional_conversions:
                if expected > 0.0:
                    conversions = Decimal(expected * fraction_jitter).quantize(
                        _MICRO, ROUND_HALF_EVEN
                    )
            else:
                conversions = Decimal(_count(expected, self.weyl(key, "conversions", ordinal)))
        conversions_value = _money(
            conversions * profile.value_per_conversion * Decimal(value_jitter)
        )
        return DayMetrics(
            cost=cost,
            impressions=impressions,
            clicks=clicks,
            conversions=conversions,
            conversions_value=conversions_value,
            form_opens=form_opens,
            leads=leads,
            landing_page_clicks=landing_page_clicks,
            website_conversions=website_conversions,
        )


# ---------------------------------------------------------------------------
# Google Ads
# ---------------------------------------------------------------------------

_GOOGLE_SERVING: Final = ("ENABLED",)
_BULK_CAMPAIGN_DAY: Final = DayMetrics(
    cost=Decimal("0.50"),
    impressions=20,
    clicks=1,
    budget_lost_is=Decimal("0.0100"),
    impression_share=Decimal("0.6000"),
)
_BULK_KEYWORD_DAY: Final = DayMetrics(cost=Decimal("0.40"), impressions=3, clicks=1)
_BULK_TERM_DAY: Final = DayMetrics(impressions=1)
_BULK_KEYWORD_QUALITY: Final = 5
_BULK_KEY: Final = "bulk-keywords"

type _IdRangeFilter = tuple[int, int] | None


def _in_range(value: int, bounds: _IdRangeFilter) -> bool:
    """Whether ``value`` lies in the inclusive ``bounds`` (always when ``None``)."""
    return bounds is None or bounds[0] <= value <= bounds[1]


class _GoogleModel:
    """The simulated Google Ads customer: entities, budgets, caps and daily facts.

    Delivery is bottom-up: a search term's traffic is part of its keyword's; a keyword's is
    its residual profile plus its matched terms; a campaign's is its keywords, its unmatched
    terms and its residual profile, the last capped by a ``DAILY`` budget (shared budgets cap
    their members jointly).
    """

    def __init__(self, core: _Core, spec: GoogleSpec) -> None:
        self.core = core
        self.spec = spec
        self.tz = ZoneInfo(spec.time_zone)
        self.budgets = {budget.key: budget for budget in spec.budgets}
        self.budget_ids = {
            budget.key: GOOGLE_BUDGET_IDS.at(index) for index, budget in enumerate(spec.budgets)
        }
        self.campaigns = {campaign.key: campaign for campaign in spec.campaigns}
        self.campaign_ids = {
            campaign.key: GOOGLE_CAMPAIGN_IDS.at(index)
            for index, campaign in enumerate(spec.campaigns)
        }
        self.ad_groups = {group.key: group for group in spec.ad_groups}
        self.ad_group_ids = {
            group.key: GOOGLE_AD_GROUP_IDS.at(index) for index, group in enumerate(spec.ad_groups)
        }
        self.keywords = {keyword.key: keyword for keyword in spec.keywords}
        self.criterion_ids = {
            keyword.key: GOOGLE_CRITERION_IDS.at(index)
            for index, keyword in enumerate(spec.keywords)
        }
        self.terms = {term.key: term for term in spec.search_terms}
        self.actions = {action.key: action for action in spec.conversion_actions}
        self.action_ids = {
            action.key: GOOGLE_CONVERSION_ACTION_IDS.at(index)
            for index, action in enumerate(spec.conversion_actions)
        }
        self.campaign_groups: dict[str, list[str]] = {key: [] for key in self.campaigns}
        for group in spec.ad_groups:
            self.campaign_groups[group.campaign_key].append(group.key)
        self.group_keywords: dict[str, list[str]] = {key: [] for key in self.ad_groups}
        for keyword in spec.keywords:
            self.group_keywords[keyword.ad_group_key].append(keyword.key)
        self.keyword_terms: dict[str, list[str]] = {key: [] for key in self.keywords}
        self.group_unmatched: dict[str, list[str]] = {key: [] for key in self.ad_groups}
        for term in spec.search_terms:
            if term.matched_keyword_key is None:
                self.group_unmatched[term.ad_group_key].append(term.key)
            else:
                self.keyword_terms[term.matched_keyword_key].append(term.key)
        bulk = spec.bulk if spec.bulk is not None else GoogleBulkSpec()
        self.bulk = bulk
        self._base_campaign = len(spec.campaigns)
        self._base_budget = len(spec.budgets)
        self._base_group = len(spec.ad_groups)
        self._base_criterion = len(spec.keywords)
        self._cache: dict[tuple[str, str, date], DayMetrics] = {}
        self._scales: dict[tuple[str, date], float] = {}
        self._lock = threading.Lock()

    # -- identifiers and layout ------------------------------------------------------------

    @property
    def bulk_keyword_campaign_id(self) -> int | None:
        """Campaign id of the dedicated ``bulk-keywords`` campaign, when it exists."""
        if not self.bulk.has_keyword_group:
            return None
        return GOOGLE_CAMPAIGN_IDS.at(self._base_campaign + self.bulk.campaigns)

    @property
    def bulk_keyword_group_id(self) -> int | None:
        """Ad group id of the dedicated ``bulk-keywords`` ad group, when it exists."""
        if not self.bulk.has_keyword_group:
            return None
        return GOOGLE_AD_GROUP_IDS.at(
            self._base_group + self.bulk.campaigns * self.bulk.ad_groups_per_campaign
        )

    def _bulk_campaign_id(self, index: int) -> int:
        return GOOGLE_CAMPAIGN_IDS.at(self._base_campaign + index)

    def _bulk_budget_amount(self) -> Decimal:
        """Daily amount of the ``bulk-keywords`` campaign's budget."""
        return max(Decimal(10), Decimal(self.bulk.keywords_in_one_ad_group))

    def all_ids(self) -> list[tuple[str, int, IdRange]]:
        """Every explicit id with its reserved range, for :meth:`SimulatedWorld.self_check`."""
        ids: list[tuple[str, int, IdRange]] = []
        ids += [("campaign", value, GOOGLE_CAMPAIGN_IDS) for value in self.campaign_ids.values()]
        ids += [("budget", value, GOOGLE_BUDGET_IDS) for value in self.budget_ids.values()]
        ids += [("ad group", value, GOOGLE_AD_GROUP_IDS) for value in self.ad_group_ids.values()]
        ids += [("criterion", value, GOOGLE_CRITERION_IDS) for value in self.criterion_ids.values()]
        ids += [
            ("conversion action", value, GOOGLE_CONVERSION_ACTION_IDS)
            for value in self.action_ids.values()
        ]
        if self.bulk.campaigns or self.bulk.has_keyword_group:
            last = self.bulk.campaigns + (1 if self.bulk.has_keyword_group else 0) - 1
            ids.append(("bulk campaign", self._bulk_campaign_id(last), GOOGLE_CAMPAIGN_IDS))
            ids.append(
                ("bulk budget", GOOGLE_BUDGET_IDS.at(self._base_budget + last), GOOGLE_BUDGET_IDS)
            )
        if self.bulk.keywords_in_one_ad_group:
            ids.append(
                (
                    "bulk criterion",
                    GOOGLE_CRITERION_IDS.at(
                        self._base_criterion + self.bulk.keywords_in_one_ad_group - 1
                    ),
                    GOOGLE_CRITERION_IDS,
                )
            )
        return ids

    # -- state at an instant ---------------------------------------------------------------

    def _status(self, state: WorldState, entity: str, key: str, initial: str) -> str:
        return str(state.value("google", entity, key, "status", initial))

    def budget_key_of(self, campaign_key: str, state: WorldState) -> str:
        """Return the budget a campaign draws from in ``state``."""
        spec = self.campaigns[campaign_key]
        return str(state.value("google", "campaign", campaign_key, "budget_key", spec.budget_key))

    def budget_amount(self, budget_key: str, state: WorldState) -> Decimal:
        """Return a budget's amount in ``state``."""
        spec = self.budgets[budget_key]
        return cast(Decimal, state.value("google", "budget", budget_key, "amount", spec.amount))

    def _day_state(self, day: date) -> WorldState:
        """The platform state at the last instant of native ``day``."""
        _start, end = self.core.day_bounds(day, self.tz)
        return self.core.state_at(end - timedelta(microseconds=1))

    def _budget_fields(
        self, budget_key: str, state: WorldState
    ) -> tuple[Decimal | None, int | None, Decimal | None, int | None]:
        """``(amount, amount_micros, total_amount, total_amount_micros)`` by budget period."""
        amount = self.budget_amount(budget_key, state)
        if self.budgets[budget_key].period == "DAILY":
            return amount, _to_micros(amount), None, None
        return None, None, amount, _to_micros(amount)

    def campaign_records(self, at: datetime) -> tuple[GoogleCampaignRecord, ...]:
        """Campaigns listed at ``at`` (every status), explicit then bulk, in id order."""
        state = self.core.state_at(at)
        records: list[GoogleCampaignRecord] = []
        for key, spec in self.campaigns.items():
            if not state.present("google", "campaign", key):
                continue
            budget_key = self.budget_key_of(key, state)
            budget = self.budgets[budget_key]
            amount, micros, total, total_micros = self._budget_fields(budget_key, state)
            records.append(
                GoogleCampaignRecord(
                    id=self.campaign_ids[key],
                    key=key,
                    name=spec.name,
                    status=self._status(state, "campaign", key, spec.status),
                    channel=spec.channel,
                    budget_id=self.budget_ids[budget_key],
                    budget_key=budget_key,
                    budget_name=budget.name or f"{budget_key} budget",
                    budget_period=budget.period,
                    budget_explicitly_shared=budget.explicitly_shared,
                    budget_amount=amount,
                    budget_amount_micros=micros,
                    budget_total_amount=total,
                    budget_total_amount_micros=total_micros,
                )
            )
        count = self.bulk.campaigns + (1 if self.bulk.has_keyword_group else 0)
        for index in range(count):
            is_keyword_campaign = index == self.bulk.campaigns
            amount = self._bulk_budget_amount() if is_keyword_campaign else Decimal(10)
            records.append(
                GoogleCampaignRecord(
                    id=self._bulk_campaign_id(index),
                    key=_BULK_KEY if is_keyword_campaign else f"bulk-campaign-{index}",
                    name="Bulk - Keywords"
                    if is_keyword_campaign
                    else f"Bulk campaign {index + 1:05d}",
                    status="ENABLED",
                    channel="SEARCH",
                    budget_id=GOOGLE_BUDGET_IDS.at(self._base_budget + index),
                    budget_key=f"bulk-budget-{index}",
                    budget_name=f"Bulk budget {index + 1:05d}",
                    budget_period="DAILY",
                    budget_explicitly_shared=False,
                    budget_amount=amount,
                    budget_amount_micros=_to_micros(amount),
                    budget_total_amount=None,
                    budget_total_amount_micros=None,
                )
            )
        return tuple(records)

    def budget_records(self, at: datetime) -> tuple[GoogleBudgetRecord, ...]:
        """Explicit budgets at ``at`` with the campaigns drawing from them, in id order."""
        state = self.core.state_at(at)
        members: dict[str, list[int]] = {key: [] for key in self.budgets}
        for key in self.campaigns:
            if state.present("google", "campaign", key):
                members[self.budget_key_of(key, state)].append(self.campaign_ids[key])
        records: list[GoogleBudgetRecord] = []
        for key, spec in self.budgets.items():
            amount, micros, total, total_micros = self._budget_fields(key, state)
            records.append(
                GoogleBudgetRecord(
                    id=self.budget_ids[key],
                    key=key,
                    name=spec.name or f"{key} budget",
                    period=spec.period,
                    explicitly_shared=spec.explicitly_shared,
                    amount=amount,
                    amount_micros=micros,
                    total_amount=total,
                    total_amount_micros=total_micros,
                    campaign_ids=tuple(sorted(members[key])),
                )
            )
        return tuple(records)

    def ad_group_records(self, at: datetime) -> tuple[GoogleAdGroupRecord, ...]:
        """Ad groups listed at ``at`` (every status), explicit then bulk, in id order."""
        state = self.core.state_at(at)
        records = [
            GoogleAdGroupRecord(
                id=self.ad_group_ids[key],
                key=key,
                campaign_id=self.campaign_ids[spec.campaign_key],
                campaign_key=spec.campaign_key,
                name=spec.name,
                status=self._status(state, "ad_group", key, spec.status),
            )
            for key, spec in self.ad_groups.items()
            if state.present("google", "ad_group", key)
            and state.present("google", "campaign", spec.campaign_key)
        ]
        per = self.bulk.ad_groups_per_campaign
        for campaign in range(self.bulk.campaigns):
            for group in range(per):
                records.append(
                    GoogleAdGroupRecord(
                        id=GOOGLE_AD_GROUP_IDS.at(self._base_group + campaign * per + group),
                        key=f"bulk-ad-group-{campaign}-{group}",
                        campaign_id=self._bulk_campaign_id(campaign),
                        campaign_key=f"bulk-campaign-{campaign}",
                        name=f"Bulk ad group {campaign + 1:05d}-{group + 1}",
                        status="ENABLED",
                    )
                )
        group_id = self.bulk_keyword_group_id
        campaign_id = self.bulk_keyword_campaign_id
        if group_id is not None and campaign_id is not None:
            records.append(
                GoogleAdGroupRecord(
                    id=group_id,
                    key=_BULK_KEY,
                    campaign_id=campaign_id,
                    campaign_key=_BULK_KEY,
                    name="Bulk keywords",
                    status="ENABLED",
                )
            )
        return tuple(records)

    def quality_score_at(self, key: str, day: date) -> int | None:
        """A keyword's current quality score on native ``day`` (Q-G7)."""
        spec = self.keywords[key]
        if spec.qs_profile is not None:
            return spec.qs_profile.score_at(self.core.offset(day))
        return spec.quality_score

    def keyword_records(self, at: datetime) -> tuple[GoogleKeywordRecord, ...]:
        """Keywords listed at ``at`` (every status, traffic or not), explicit then bulk."""
        state = self.core.state_at(at)
        native = _require_aware("at", at).astimezone(self.tz).date()
        records: list[GoogleKeywordRecord] = []
        for key, spec in self.keywords.items():
            group = self.ad_groups[spec.ad_group_key]
            if not (
                state.present("google", "keyword", key)
                and state.present("google", "ad_group", group.key)
                and state.present("google", "campaign", group.campaign_key)
            ):
                continue
            records.append(
                GoogleKeywordRecord(
                    criterion_id=self.criterion_ids[key],
                    key=key,
                    ad_group_id=self.ad_group_ids[group.key],
                    campaign_id=self.campaign_ids[group.campaign_key],
                    text=spec.text,
                    match_type=spec.match_type,
                    status=self._status(state, "keyword", key, spec.status),
                    quality_score=self.quality_score_at(key, native),
                )
            )
        group_id = self.bulk_keyword_group_id
        campaign_id = self.bulk_keyword_campaign_id
        if group_id is not None and campaign_id is not None:
            for index in range(self.bulk.keywords_in_one_ad_group):
                records.append(
                    GoogleKeywordRecord(
                        criterion_id=GOOGLE_CRITERION_IDS.at(self._base_criterion + index),
                        key=f"bulk-keyword-{index}",
                        ad_group_id=group_id,
                        campaign_id=campaign_id,
                        text=f"bulk keyword {index + 1:05d}",
                        match_type="EXACT",
                        status="ENABLED",
                        quality_score=_BULK_KEYWORD_QUALITY,
                    )
                )
        return tuple(records)

    def conversion_action_records(self, at: datetime) -> tuple[GoogleConversionActionRecord, ...]:
        """Every conversion action at ``at``, removed and hidden ones included (Q-G8)."""
        native = _require_aware("at", at).astimezone(self.tz).date()
        offset = self.core.offset(native)
        return tuple(
            GoogleConversionActionRecord(
                id=self.action_ids[key],
                key=key,
                name=spec.name,
                status=spec.status,
                include_in_conversions_metric=spec.include_in_conversions_metric,
                counting_type=spec.counting_type,
                attribution_model=spec.model_at(offset),
            )
            for key, spec in self.actions.items()
        )

    # -- delivery gates ----------------------------------------------------------------------

    def _campaign_allows(self, key: str, day: date, bounded: bool) -> bool:
        spec = self.campaigns[key]
        return self.core.status_allows(
            "google",
            "campaign",
            key,
            spec.status,
            _GOOGLE_SERVING,
            bounded=bounded,
            day=day,
            tz=self.tz,
        )

    def _ad_group_allows(self, key: str, day: date, bounded: bool) -> bool:
        spec = self.ad_groups[key]
        return self.core.status_allows(
            "google",
            "ad_group",
            key,
            spec.status,
            _GOOGLE_SERVING,
            bounded=bounded,
            day=day,
            tz=self.tz,
        ) and self._campaign_allows(spec.campaign_key, day, bounded)

    def _keyword_allows(self, key: str, day: date, bounded: bool) -> bool:
        spec = self.keywords[key]
        return self.core.status_allows(
            "google",
            "keyword",
            key,
            spec.status,
            _GOOGLE_SERVING,
            bounded=bounded,
            day=day,
            tz=self.tz,
        ) and self._ad_group_allows(spec.ad_group_key, day, bounded)

    # -- full-day facts ------------------------------------------------------------------------

    def _cached(self, kind: str, key: str, day: date) -> DayMetrics | None:
        return self._cache.get((kind, key, day))

    def _store(self, kind: str, key: str, day: date, metrics: DayMetrics) -> DayMetrics:
        with self._lock:
            self._cache[(kind, key, day)] = metrics
        return metrics

    def _evaluate(
        self, namespace: str, key: str, profile: Profile, day: date, scale: float
    ) -> DayMetrics:
        if isinstance(profile, ExplicitDays):
            return profile.at(self.core.offset(day))
        return self.core.profile_day(
            f"google:{namespace}:{key}", profile, day, scale=scale, kind="google"
        )

    def term_full(self, key: str, day: date) -> DayMetrics:
        """A search term's full-day metrics on native ``day``."""
        cached = self._cached("term", key, day)
        if cached is not None:
            return cached
        term = self.terms[key]
        metrics = _ZERO_DAY
        if term.profile is not None:
            bounded = _bounded(term.profile)
            if term.matched_keyword_key is not None:
                allowed = self._keyword_allows(term.matched_keyword_key, day, bounded)
            else:
                allowed = self._ad_group_allows(term.ad_group_key, day, bounded)
            if allowed:
                metrics = self._evaluate("term", key, term.profile, day, 1.0)
        return self._store("term", key, day, metrics)

    def keyword_full(self, key: str, day: date) -> DayMetrics:
        """A keyword's full-day metrics: residual plus its matched terms, with QS and CPC."""
        cached = self._cached("keyword", key, day)
        if cached is not None:
            return cached
        spec = self.keywords[key]
        residual = _ZERO_DAY
        if spec.profile is not None and self._keyword_allows(key, day, _bounded(spec.profile)):
            residual = self._evaluate("keyword", key, spec.profile, day, 1.0)
        total = residual
        for term_key in self.keyword_terms[key]:
            term = self.term_full(term_key, day)
            if not term.is_zero:
                total = _plus(total, term)
        if total.is_zero:
            return self._store("keyword", key, day, _ZERO_DAY)
        quality = residual.historical_quality_score
        if quality is None:
            quality = self._historical_quality(key, day)
        average = residual.average_cpc
        if average is None and total.clicks > 0:
            average = Decimal(_to_micros(total.cost) // total.clicks) / _MICROS_PER_UNIT
        return self._store(
            "keyword",
            key,
            day,
            total.with_(historical_quality_score=quality, average_cpc=average),
        )

    def _historical_quality(self, key: str, day: date) -> int | None:
        """The historical quality score a delivering keyword reports on native ``day``."""
        spec = self.keywords[key]
        profile = spec.qs_profile
        if profile is None:
            return spec.quality_score
        share = float(profile.present_share)
        if share < 1.0 and self.core.weyl(f"google:qs:{key}", "present", day.toordinal()) >= share:
            return None
        return profile.score_at(self.core.offset(day))

    def _children(self, key: str, day: date) -> DayMetrics:
        """A campaign's keyword and unmatched-term traffic on native ``day``."""
        cached = self._cached("children", key, day)
        if cached is not None:
            return cached
        total = _ZERO_DAY
        for group_key in self.campaign_groups[key]:
            for keyword_key in self.group_keywords[group_key]:
                keyword = self.keyword_full(keyword_key, day)
                if not keyword.is_zero:
                    total = _plus(total, keyword)
            for term_key in self.group_unmatched[group_key]:
                term = self.term_full(term_key, day)
                if not term.is_zero:
                    total = _plus(total, term)
        return self._store("children", key, day, total)

    def _residual_demand(self, key: str, day: date) -> DayMetrics:
        """A campaign's uncapped residual delivery on native ``day``."""
        profile = self.campaigns[key].profile
        if profile is None or not self._campaign_allows(key, day, _bounded(profile)):
            return _ZERO_DAY
        return self._evaluate("campaign", key, profile, day, 1.0)

    def _cap_draw(self, namespace: str, key: str, day: date) -> float:
        """The share of the budget a capped day aims at, in [0.975, 1.015).

        Rounding the scaled impressions and clicks to whole numbers moves the realised spend
        around it.
        """
        return 0.975 + 0.04 * self.core.day_rng(namespace, key, day).random()

    def _shared_scale(self, budget_key: str, day: date) -> float:
        """The residual scale of every member of a shared budget on native ``day``."""
        cached = self._scales.get((budget_key, day))
        if cached is not None:
            return cached
        state = self._day_state(day)
        cap = float(self.budget_amount(budget_key, state))
        children = 0.0
        demand = 0.0
        for key, spec in self.campaigns.items():
            if self.budget_key_of(key, state) != budget_key:
                continue
            children += float(self._children(key, day).cost)
            if isinstance(spec.profile, DailyProfile):
                demand += float(self._residual_demand(key, day).cost)
            elif isinstance(spec.profile, ExplicitDays):
                children += float(self._residual_demand(key, day).cost)
        scale = 1.0
        if demand > 0.0 and children + demand > cap:
            target = cap * self._cap_draw("google:shared-cap", budget_key, day) - children
            scale = max(0.0, target) / demand
        scale = min(scale, 1.0)
        with self._lock:
            self._scales[(budget_key, day)] = scale
        return scale

    def _residual(self, key: str, day: date, children: DayMetrics) -> tuple[DayMetrics, bool]:
        """A campaign's residual delivery after its budget cap, and whether it was capped."""
        spec = self.campaigns[key]
        demand = self._residual_demand(key, day)
        if demand.is_zero or not isinstance(spec.profile, DailyProfile):
            return demand, False
        state = self._day_state(day)
        budget_key = self.budget_key_of(key, state)
        budget = self.budgets[budget_key]
        if budget.period != "DAILY":
            return demand, False
        if budget.explicitly_shared:
            scale = self._shared_scale(budget_key, day)
        else:
            cap = float(self.budget_amount(budget_key, state))
            if float(children.cost + demand.cost) <= cap:
                return demand, False
            target = cap * self._cap_draw("google:cap", key, day) - float(children.cost)
            scale = max(0.0, target) / float(demand.cost)
        if scale >= 1.0:
            return demand, False
        capped = self.core.profile_day(
            f"google:campaign:{key}", spec.profile, day, scale=scale, kind="google"
        )
        return capped, True

    def campaign_full(self, key: str, day: date) -> DayMetrics:
        """A campaign's full-day metrics on native ``day``, impression shares included."""
        cached = self._cached("campaign", key, day)
        if cached is not None:
            return cached
        spec = self.campaigns[key]
        children = self._children(key, day)
        residual, capped = self._residual(key, day, children)
        total = _plus(children, residual) if not residual.is_zero else children
        if total.is_zero and total.conversions == 0:
            return self._store("campaign", key, day, _ZERO_DAY)
        lost: Decimal | None = None
        share: Decimal | None = None
        if spec.channel == "SEARCH":
            if isinstance(spec.profile, ExplicitDays) and (
                residual.budget_lost_is is not None or residual.impression_share is not None
            ):
                lost, share = residual.budget_lost_is, residual.impression_share
            else:
                state = self._day_state(day)
                budget_key = self.budget_key_of(key, state)
                daily = self.budgets[budget_key].period == "DAILY"
                cap = self.budget_amount(budget_key, state)
                if budget_key in self.budgets and self.budgets[budget_key].explicitly_shared:
                    cap_hit = self._shared_scale(budget_key, day) < 1.0
                else:
                    cap_hit = capped or (daily and total.cost >= cap * Decimal("0.95"))
                rng = self.core.day_rng("google:impression-share", key, day)
                lost_value = 0.12 + 0.18 * rng.random() if cap_hit else 0.04 * rng.random()
                rank_lost = 0.05 + 0.20 * rng.random()
                lost = _share(lost_value)
                share = _share(max(0.05, 1.0 - lost_value - rank_lost))
        return self._store(
            "campaign", key, day, total.with_(budget_lost_is=lost, impression_share=share)
        )

    # -- daily reads ------------------------------------------------------------------------

    def campaign_days(
        self, start: date, end: date, campaign_ids: _IdRangeFilter
    ) -> tuple[GoogleCampaignDay, ...]:
        """Q-G3 rows: one per delivering campaign and native date, in (date, id) order."""
        rows: list[GoogleCampaignDay] = []
        for day in self.core.days(start, end, self.tz):
            fraction = self.core.fraction(day, self.tz)
            for key in self.campaigns:
                campaign_id = self.campaign_ids[key]
                if not _in_range(campaign_id, campaign_ids):
                    continue
                metrics = _partial(self.campaign_full(key, day), fraction)
                if not metrics.is_zero or metrics.conversions:
                    rows.append(self._campaign_row(campaign_id, day, metrics))
            for index, metrics in self._bulk_campaign_metrics(campaign_ids):
                partial = _partial(metrics, fraction)
                if not partial.is_zero:
                    rows.append(self._campaign_row(self._bulk_campaign_id(index), day, partial))
        rows.sort(key=lambda row: (row.date, row.campaign_id))
        return tuple(rows)

    def _bulk_campaign_metrics(self, campaign_ids: _IdRangeFilter) -> list[tuple[int, DayMetrics]]:
        """Fixed daily metrics of the bulk campaigns whose ids fall in ``campaign_ids``."""
        count = self.bulk.campaigns
        if count == 0 and not self.bulk.has_keyword_group:
            return []
        first = self._bulk_campaign_id(0)
        low, high = (first, first + count) if campaign_ids is None else campaign_ids
        start = max(0, low - first)
        stop = min(count, high - first + 1)
        result = [(index, _BULK_CAMPAIGN_DAY) for index in range(start, max(start, stop))]
        keyword_campaign = self.bulk_keyword_campaign_id
        if keyword_campaign is not None and _in_range(keyword_campaign, campaign_ids):
            keywords = self.bulk.keywords_in_one_ad_group
            terms = self.bulk.search_term_rows_per_day
            unmatched = terms if keywords == 0 else 0
            metrics = DayMetrics(
                cost=_BULK_KEYWORD_DAY.cost * keywords,
                impressions=_BULK_KEYWORD_DAY.impressions * keywords + unmatched,
                clicks=_BULK_KEYWORD_DAY.clicks * keywords,
                budget_lost_is=Decimal("0.0100"),
                impression_share=Decimal("0.6000"),
            )
            result.append((count, metrics))
        return result

    def _campaign_row(self, campaign_id: int, day: date, metrics: DayMetrics) -> GoogleCampaignDay:
        return GoogleCampaignDay(
            campaign_id=campaign_id,
            date=day,
            cost=metrics.cost,
            cost_micros=_to_micros(metrics.cost),
            impressions=metrics.impressions,
            clicks=metrics.clicks,
            conversions=metrics.conversions,
            conversions_value=metrics.conversions_value,
            search_budget_lost_impression_share=metrics.budget_lost_is,
            search_impression_share=metrics.impression_share,
        )

    def keyword_days(
        self,
        start: date,
        end: date,
        campaign_ids: _IdRangeFilter,
        ad_group_ids: _IdRangeFilter,
    ) -> tuple[GoogleKeywordDay, ...]:
        """Q-G5 rows: one per delivering keyword and native date.

        Ordered by (date, ad group, criterion).
        """
        rows: list[GoogleKeywordDay] = []
        for day in self.core.days(start, end, self.tz):
            fraction = self.core.fraction(day, self.tz)
            for key, spec in self.keywords.items():
                group = self.ad_groups[spec.ad_group_key]
                group_id = self.ad_group_ids[group.key]
                campaign_id = self.campaign_ids[group.campaign_key]
                if not (_in_range(campaign_id, campaign_ids) and _in_range(group_id, ad_group_ids)):
                    continue
                full = self.keyword_full(key, day)
                metrics = _partial(full, fraction)
                if metrics.is_zero:
                    continue
                rows.append(
                    self._keyword_row(self.criterion_ids[key], group_id, campaign_id, day, metrics)
                )
            bulk_group = self.bulk_keyword_group_id
            bulk_campaign = self.bulk_keyword_campaign_id
            if (
                bulk_group is not None
                and bulk_campaign is not None
                and _in_range(bulk_campaign, campaign_ids)
                and _in_range(bulk_group, ad_group_ids)
            ):
                metrics = _partial(
                    _BULK_KEYWORD_DAY.with_(
                        historical_quality_score=_BULK_KEYWORD_QUALITY,
                        average_cpc=_BULK_KEYWORD_DAY.cost,
                    ),
                    fraction,
                )
                if not metrics.is_zero:
                    for index in range(self.bulk.keywords_in_one_ad_group):
                        rows.append(
                            self._keyword_row(
                                GOOGLE_CRITERION_IDS.at(self._base_criterion + index),
                                bulk_group,
                                bulk_campaign,
                                day,
                                metrics,
                            )
                        )
        rows.sort(key=lambda row: (row.date, row.ad_group_id, row.criterion_id))
        return tuple(rows)

    def _keyword_row(
        self, criterion_id: int, group_id: int, campaign_id: int, day: date, metrics: DayMetrics
    ) -> GoogleKeywordDay:
        average = metrics.average_cpc
        return GoogleKeywordDay(
            criterion_id=criterion_id,
            ad_group_id=group_id,
            campaign_id=campaign_id,
            date=day,
            cost=metrics.cost,
            cost_micros=_to_micros(metrics.cost),
            impressions=metrics.impressions,
            clicks=metrics.clicks,
            conversions=metrics.conversions,
            historical_quality_score=metrics.historical_quality_score,
            average_cpc=average,
            average_cpc_micros=None if average is None else _to_micros(average),
        )

    def search_term_days(
        self,
        start: date,
        end: date,
        campaign_ids: _IdRangeFilter,
        ad_group_ids: _IdRangeFilter,
    ) -> tuple[GoogleSearchTermDay, ...]:
        """Q-G6 rows: one per search term with impressions and native date.

        Ordered by (date, ad group, term).
        """
        rows: list[GoogleSearchTermDay] = []
        for day in self.core.days(start, end, self.tz):
            fraction = self.core.fraction(day, self.tz)
            for key, term in self.terms.items():
                group = self.ad_groups[term.ad_group_key]
                group_id = self.ad_group_ids[group.key]
                campaign_id = self.campaign_ids[group.campaign_key]
                if not (_in_range(campaign_id, campaign_ids) and _in_range(group_id, ad_group_ids)):
                    continue
                metrics = _partial(self.term_full(key, day), fraction)
                if metrics.impressions == 0 and metrics.cost == 0:
                    continue
                keyword = (
                    None
                    if term.matched_keyword_key is None
                    else self.keywords[term.matched_keyword_key]
                )
                rows.append(
                    GoogleSearchTermDay(
                        search_term=term.term,
                        status=term.status,
                        ad_group_id=group_id,
                        campaign_id=campaign_id,
                        criterion_id=None if keyword is None else self.criterion_ids[keyword.key],
                        keyword_text=None if keyword is None else keyword.text,
                        match_type=None if keyword is None else keyword.match_type,
                        date=day,
                        cost=metrics.cost,
                        cost_micros=_to_micros(metrics.cost),
                        impressions=metrics.impressions,
                        clicks=metrics.clicks,
                        conversions=metrics.conversions,
                        conversions_value=metrics.conversions_value,
                    )
                )
            rows.extend(self._bulk_term_rows(day, fraction, campaign_ids, ad_group_ids))
        rows.sort(key=lambda row: (row.date, row.ad_group_id, row.search_term))
        return tuple(rows)

    def _bulk_term_rows(
        self,
        day: date,
        fraction: Decimal,
        campaign_ids: _IdRangeFilter,
        ad_group_ids: _IdRangeFilter,
    ) -> list[GoogleSearchTermDay]:
        group_id = self.bulk_keyword_group_id
        campaign_id = self.bulk_keyword_campaign_id
        count = self.bulk.search_term_rows_per_day
        if (
            group_id is None
            or campaign_id is None
            or count == 0
            or not _in_range(campaign_id, campaign_ids)
            or not _in_range(group_id, ad_group_ids)
        ):
            return []
        metrics = _partial(_BULK_TERM_DAY, fraction)
        if metrics.impressions == 0:
            return []
        keywords = self.bulk.keywords_in_one_ad_group
        rows: list[GoogleSearchTermDay] = []
        for index in range(count):
            matched = index % keywords if keywords else None
            rows.append(
                GoogleSearchTermDay(
                    search_term=f"bulk search term {index + 1:05d}",
                    status="NONE",
                    ad_group_id=group_id,
                    campaign_id=campaign_id,
                    criterion_id=None
                    if matched is None
                    else GOOGLE_CRITERION_IDS.at(self._base_criterion + matched),
                    keyword_text=None if matched is None else f"bulk keyword {matched + 1:05d}",
                    match_type=None if matched is None else "EXACT",
                    date=day,
                    cost=metrics.cost,
                    cost_micros=_to_micros(metrics.cost),
                    impressions=metrics.impressions,
                    clicks=metrics.clicks,
                    conversions=metrics.conversions,
                    conversions_value=metrics.conversions_value,
                )
            )
        return rows

    def daily_budget_on(self, key: str, day: date) -> Decimal | None:
        """A campaign's daily budget on native ``day`` (``None`` for lifetime budgets)."""
        state = self._day_state(day)
        budget_key = self.budget_key_of(key, state)
        if self.budgets[budget_key].period != "DAILY":
            return None
        return self.budget_amount(budget_key, state)


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------

_LINKEDIN_SERVING: Final = ("ACTIVE",)
_COUNTRIES: Final = ("Germany", "France", "United Kingdom", "Netherlands", "Spain", "Sweden")
_CHALLENGES: Final = (
    "Proving pipeline from paid social",
    "Connecting ad spend to revenue",
    "Getting sales to trust the numbers",
    "Too many disconnected tools",
)


@dataclass(slots=True)
class _Person:
    """A simulated person behind one or more lead responses (internal, append-only)."""

    index: int
    key: str
    email: str
    email_sha256: str
    first_name: str
    last_name: str
    first_touch: datetime
    touches: list[datetime]


@dataclass(frozen=True, slots=True)
class _RawLead:
    """A generated lead response before its person is assigned."""

    id: str
    creative_key: str
    submitted_at: datetime
    test_lead: bool
    hide_creative: bool
    first_name: str
    last_name: str
    company: str
    title: str
    country: str
    challenge: str


def _response_id(digest: str) -> str:
    """Format a hex digest as a plain LinkedIn-style response id (not a URN)."""
    return (
        f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        f"-{int(digest[32], 16) % 10}"
    )


class _LinkedInModel:
    """The simulated LinkedIn advertiser: entities, capped delivery, segments, forms and leads.

    Lead responses are generated per creative and UTC day from that day's ``leads``, so the
    daily analytics and the Lead Sync responses always agree. People are assigned to responses
    in chronological order, day by day, so a repeat lead only ever reuses an earlier person and
    every assignment is stable however far the clock has advanced.
    """

    def __init__(self, core: _Core, spec: LinkedInSpec) -> None:
        self.core = core
        self.spec = spec
        self.accounts = {account.id: account for account in spec.accounts}
        self.groups = {group.key: group for group in spec.campaign_groups}
        self.group_ids = {
            group.key: LINKEDIN_CAMPAIGN_GROUP_IDS.at(index)
            for index, group in enumerate(spec.campaign_groups)
        }
        self.campaigns = {campaign.key: campaign for campaign in spec.campaigns}
        self.campaign_ids = {
            campaign.key: LINKEDIN_CAMPAIGN_IDS.at(index)
            for index, campaign in enumerate(spec.campaigns)
        }
        self.creatives = {creative.key: creative for creative in spec.creatives}
        self.creative_ids: dict[str, int] = {}
        for index, creative in enumerate(spec.creatives):
            self.creative_ids[creative.key] = (
                creative.id if creative.id is not None else LINKEDIN_CREATIVE_IDS.at(index)
            )
        _check_unique("LinkedIn creative id", self.creative_ids.values())
        self.creative_by_id = {value: key for key, value in self.creative_ids.items()}
        self.post_ids = {
            creative.key: LINKEDIN_POST_IDS.at(index)
            for index, creative in enumerate(spec.creatives)
        }
        self.forms = {form.id: form for form in spec.lead_forms}
        self.campaign_creatives: dict[str, list[str]] = {key: [] for key in self.campaigns}
        for creative in spec.creatives:
            self.campaign_creatives[creative.campaign_key].append(creative.key)
        self.segments: dict[tuple[str, str], LinkedInSegmentSpec] = {
            (segment.campaign_key, segment.pivot): segment for segment in spec.segments
        }
        self.lead_creatives = [
            creative.key
            for creative in spec.creatives
            if creative.lead_form_id is not None and creative.profile is not None
        ]
        self._history_instant = _midnight(core.history_start)
        self._cache: dict[tuple[str, date], DayMetrics] = {}
        self._scales: dict[tuple[str, date], tuple[float, Decimal | None]] = {}
        self._capped: dict[tuple[str, date], Mapping[str, DayMetrics]] = {}
        self._lock = threading.RLock()
        self._leads_through: date | None = None
        funnel = core.spec.hubspot.funnel if core.spec.hubspot is not None else FunnelSpec()
        self._repeat_share = float(funnel.repeat_person_share)
        self.persons: list[_Person] = []
        self.person_by_hash: dict[str, _Person] = {}
        self.responses_by_id: dict[str, LinkedInLeadResponse] = {}
        self.responses_by_day: dict[date, list[LinkedInLeadResponse]] = {}
        self.responses_by_creative_day: dict[tuple[str, date], list[LinkedInLeadResponse]] = {}

    # -- identifiers ---------------------------------------------------------------------------

    def created_at(self, index: int) -> datetime:
        """Deterministic creation instant of the ``index``-th entity of a kind."""
        return self._history_instant + timedelta(hours=9, minutes=index)

    def all_ids(self) -> list[tuple[str, int, IdRange]]:
        """Every id with its reserved range, for :meth:`SimulatedWorld.self_check`."""
        ids: list[tuple[str, int, IdRange]] = []
        ids += [("account", value, LINKEDIN_ACCOUNT_IDS) for value in self.accounts]
        ids += [
            ("campaign group", value, LINKEDIN_CAMPAIGN_GROUP_IDS)
            for value in self.group_ids.values()
        ]
        ids += [("campaign", value, LINKEDIN_CAMPAIGN_IDS) for value in self.campaign_ids.values()]
        ids += [("creative", value, LINKEDIN_CREATIVE_IDS) for value in self.creative_ids.values()]
        ids += [("post", value, LINKEDIN_POST_IDS) for value in self.post_ids.values()]
        ids += [("lead form", value, LINKEDIN_FORM_IDS) for value in self.forms]
        return ids

    def account_of(self, value: int | str) -> LinkedInAccountSpec:
        """Return the account spec for an id or account URN.

        Raises:
            KeyError: When the world has no such account.
        """
        account_id = _plain_id(value, "urn:li:sponsoredAccount:", "account_id")
        account = self.accounts.get(account_id)
        if account is None:
            raise KeyError(f"no simulated LinkedIn account {account_id}")
        return account

    # -- entities at an instant -------------------------------------------------------------------

    def _version_tag(self, entity: str, key: str, at: datetime) -> tuple[str, datetime | None]:
        changes = self.core.applied_changes("linkedin", entity, key, at)
        last = changes[-1].instant(self.core.anchor) if changes else None
        return str(1 + len(changes)), last

    def group_records(
        self, account_id: int, at: datetime
    ) -> tuple[LinkedInCampaignGroupRecord, ...]:
        """Campaign groups of an account listed at ``at`` (every status), in id order."""
        state = self.core.state_at(at)
        account = self.accounts[account_id]
        records: list[LinkedInCampaignGroupRecord] = []
        for index, (key, spec) in enumerate(self.groups.items()):
            if spec.account_id != account_id or not state.present(
                "linkedin", "campaign_group", key
            ):
                continue
            created = self.created_at(index)
            _tag, last = self._version_tag("campaign_group", key, at)
            start_offset = (
                spec.run_start_offset
                if spec.run_start_offset is not None
                else self.core.history_start_offset
            )
            records.append(
                LinkedInCampaignGroupRecord(
                    id=self.group_ids[key],
                    key=key,
                    account_id=account_id,
                    name=spec.name,
                    status=str(
                        state.value("linkedin", "campaign_group", key, "status", spec.status)
                    ),
                    daily_budget=cast(
                        Decimal | None,
                        state.value(
                            "linkedin", "campaign_group", key, "daily_budget", spec.daily_budget
                        ),
                    ),
                    total_budget=cast(
                        Decimal | None,
                        state.value(
                            "linkedin", "campaign_group", key, "total_budget", spec.total_budget
                        ),
                    ),
                    currency=account.currency,
                    run_start=_midnight(self.core.anchor + timedelta(days=start_offset)),
                    run_end=None
                    if spec.run_end_offset is None
                    else _midnight(self.core.anchor + timedelta(days=spec.run_end_offset)),
                    created_at=created,
                    last_modified_at=last or created,
                )
            )
        return tuple(sorted(records, key=lambda record: record.id))

    def daily_budget_at(self, key: str, state: WorldState) -> Decimal | None:
        """A campaign's daily budget in ``state``."""
        spec = self.campaigns[key]
        return cast(
            Decimal | None,
            state.value("linkedin", "campaign", key, "daily_budget", spec.daily_budget),
        )

    def campaign_records(self, account_id: int, at: datetime) -> tuple[LinkedInCampaignRecord, ...]:
        """Campaigns of an account listed at ``at`` (every status, ``COMPLETED`` included)."""
        state = self.core.state_at(at)
        account = self.accounts[account_id]
        records: list[LinkedInCampaignRecord] = []
        for index, (key, spec) in enumerate(self.campaigns.items()):
            if spec.account_id != account_id or not state.present("linkedin", "campaign", key):
                continue
            created = self.created_at(100 + index)
            tag, last = self._version_tag("campaign", key, at)
            records.append(
                LinkedInCampaignRecord(
                    id=self.campaign_ids[key],
                    key=key,
                    account_id=account_id,
                    group_id=self.group_ids[spec.group_key],
                    name=spec.name,
                    status=str(state.value("linkedin", "campaign", key, "status", spec.status)),
                    objective=spec.objective,
                    daily_budget=self.daily_budget_at(key, state),
                    total_budget=cast(
                        Decimal | None,
                        state.value("linkedin", "campaign", key, "total_budget", spec.total_budget),
                    ),
                    currency=account.currency,
                    run_start=created,
                    created_at=created,
                    last_modified_at=last or created,
                    version_tag=tag,
                )
            )
        return tuple(sorted(records, key=lambda record: record.id))

    def creative_records(
        self, account_id: int, campaign_ids: Iterable[int] | None, at: datetime
    ) -> tuple[LinkedInCreativeRecord, ...]:
        """Creatives of an account at ``at``, optionally only those of ``campaign_ids``."""
        state = self.core.state_at(at)
        wanted = None if campaign_ids is None else set(campaign_ids)
        records: list[LinkedInCreativeRecord] = []
        for index, (key, spec) in enumerate(self.creatives.items()):
            campaign = self.campaigns[spec.campaign_key]
            campaign_id = self.campaign_ids[spec.campaign_key]
            if campaign.account_id != account_id or (
                wanted is not None and campaign_id not in wanted
            ):
                continue
            if not (
                state.present("linkedin", "creative", key)
                and state.present("linkedin", "campaign", spec.campaign_key)
            ):
                continue
            created = self.created_at(200 + index)
            _tag, last = self._version_tag("creative", key, at)
            records.append(
                LinkedInCreativeRecord(
                    id=self.creative_ids[key],
                    key=key,
                    account_id=account_id,
                    campaign_id=campaign_id,
                    intended_status=str(
                        state.value("linkedin", "creative", key, "status", spec.intended_status)
                    ),
                    post_id=self.post_ids[key],
                    headline=spec.headline,
                    intro_text=spec.intro_text,
                    cta_label=spec.cta_label,
                    lead_form_id=spec.lead_form_id,
                    landing_page_url=spec.landing_page_url,
                    created_at=created,
                    last_modified_at=last or created,
                )
            )
        return tuple(sorted(records, key=lambda record: record.id))

    def post_records(self, urns: Iterable[str]) -> tuple[LinkedInPostRecord, ...]:
        """Posts for ``urns`` (``urn:li:share:{id}``), known ones only, in first-seen order."""
        by_id = {value: key for key, value in self.post_ids.items()}
        records: list[LinkedInPostRecord] = []
        seen: set[str] = set()
        for urn in urns:
            if urn in seen or not urn.startswith("urn:li:share:"):
                continue
            seen.add(urn)
            tail = urn.removeprefix("urn:li:share:")
            if not tail.isdecimal():
                continue
            key = by_id.get(int(tail))
            if key is None:
                continue
            spec = self.creatives[key]
            records.append(
                LinkedInPostRecord(
                    id=self.post_ids[key],
                    account_id=self.campaigns[spec.campaign_key].account_id,
                    headline=spec.headline,
                    commentary=spec.intro_text,
                    landing_page_url=spec.landing_page_url,
                    created_at=self.created_at(200 + list(self.creatives).index(key)),
                )
            )
        return tuple(records)

    # -- delivery ----------------------------------------------------------------------------------

    def _allows(self, key: str, day: date) -> bool:
        spec = self.creatives[key]
        bounded = _bounded(spec.profile)
        campaign = self.campaigns[spec.campaign_key]
        return self.core.status_allows(
            "linkedin",
            "creative",
            key,
            spec.intended_status,
            _LINKEDIN_SERVING,
            bounded=bounded,
            day=day,
            tz=None,
        ) and self.core.status_allows(
            "linkedin",
            "campaign",
            spec.campaign_key,
            campaign.status,
            _LINKEDIN_SERVING,
            bounded=bounded,
            day=day,
            tz=None,
        )

    def _kind(self, key: str) -> _ProfileKind:
        return "lead_gen" if self.creatives[key].lead_form_id is not None else "landing"

    def _demand(self, key: str, day: date) -> DayMetrics:
        spec = self.creatives[key]
        if spec.profile is None or not self._allows(key, day):
            return _ZERO_DAY
        if isinstance(spec.profile, ExplicitDays):
            return spec.profile.at(self.core.offset(day))
        return self.core.profile_day(
            f"linkedin:creative:{key}", spec.profile, day, scale=1.0, kind=self._kind(key)
        )

    def _campaign_cap(self, campaign_key: str, day: date) -> tuple[float, Decimal | None]:
        """The delivery scale of a campaign's profile creatives under its daily budget.

        Returns:
            The scale, and on a capped day the spend its profile creatives are priced at: the
            drawn 96-100% share of the budget rounded up to the cent, less the stated spend of
            its ``ExplicitDays`` creatives (never below zero); ``None`` on an uncapped day.
        """
        cached = self._scales.get((campaign_key, day))
        if cached is not None:
            return cached
        _start, end = self.core.day_bounds(day, None)
        daily = self.daily_budget_at(
            campaign_key, self.core.state_at(end - timedelta(microseconds=1))
        )
        scale = 1.0
        target: Decimal | None = None
        if daily is not None:
            demand = 0.0
            fixed = Decimal(0)
            for key in self.campaign_creatives[campaign_key]:
                cost = self._demand(key, day).cost
                if isinstance(self.creatives[key].profile, DailyProfile):
                    demand += float(cost)
                else:
                    fixed += cost
            if demand > 0.0 and demand + float(fixed) > float(daily):
                draw = 0.96 + 0.04 * self.core.day_rng("linkedin:cap", campaign_key, day).random()
                scale = max(0.0, float(daily) * draw - float(fixed)) / demand
                share = (daily * Decimal(draw)).quantize(_CENT, ROUND_CEILING)
                target = max(Decimal(0), share - fixed)
        cap = (min(scale, 1.0), target)
        with self._lock:
            self._scales[(campaign_key, day)] = cap
        return cap

    def _capped_delivery(
        self, campaign_key: str, day: date, scale: float, target: Decimal
    ) -> Mapping[str, DayMetrics]:
        """The full-day metrics of a capped campaign's serving profile creatives on UTC ``day``.

        Each creative's demand is scaled by ``scale`` and rounded to whole impressions and
        clicks, then ``target`` is split over what was bought, in proportion to each creative's
        rounded cost (to its impressions when the budget bought no click). Rounding thus moves
        the cost per click, never the campaign's spend, and every count, leads included, is
        exactly the scaled one.
        """
        cached = self._capped.get((campaign_key, day))
        if cached is not None:
            return cached
        delivered: dict[str, DayMetrics] = {}
        for key in self.campaign_creatives[campaign_key]:
            profile = self.creatives[key].profile
            if isinstance(profile, DailyProfile) and self._allows(key, day):
                delivered[key] = self.core.profile_day(
                    f"linkedin:creative:{key}", profile, day, scale=scale, kind=self._kind(key)
                )
        weights = {key: int(metrics.cost / _CENT) for key, metrics in delivered.items()}
        if not any(weights.values()):
            weights = {key: metrics.impressions for key, metrics in delivered.items()}
        total = sum(weights.values())
        if total > 0:
            # Whole cents, or micros when explicit spend carries fractions of a cent; integer
            # floors keep every share non-negative and the shares summing exactly to the target.
            unit = _CENT if target == target.quantize(_CENT) else _MICRO
            units = int(target / unit)
            payers = [key for key, weight in weights.items() if weight > 0]
            remaining = units
            for key in payers[:-1]:
                share = units * weights[key] // total
                delivered[key] = delivered[key].with_(cost=share * unit)
                remaining -= share
            delivered[payers[-1]] = delivered[payers[-1]].with_(cost=remaining * unit)
        result = MappingProxyType(delivered)
        with self._lock:
            self._capped[(campaign_key, day)] = result
        return result

    def creative_full(self, key: str, day: date) -> DayMetrics:
        """A creative's full-day metrics on UTC ``day``, after its campaign's budget cap."""
        cached = self._cache.get((key, day))
        if cached is not None:
            return cached
        spec = self.creatives[key]
        metrics = _ZERO_DAY
        if spec.profile is not None and self._allows(key, day):
            if isinstance(spec.profile, ExplicitDays):
                metrics = spec.profile.at(self.core.offset(day))
            else:
                scale, target = self._campaign_cap(spec.campaign_key, day)
                if target is None:
                    metrics = self.core.profile_day(
                        f"linkedin:creative:{key}",
                        spec.profile,
                        day,
                        scale=scale,
                        kind=self._kind(key),
                    )
                else:
                    metrics = self._capped_delivery(spec.campaign_key, day, scale, target)[key]
            if spec.lead_form_id is None and metrics.leads:
                metrics = metrics.with_(leads=0)
        with self._lock:
            self._cache[(key, day)] = metrics
        return metrics

    def creative_days(
        self, account_id: int, start: date, end: date
    ) -> tuple[LinkedInCreativeDay, ...]:
        """Daily rows of an account's delivering creatives, in (date, creative id) order.

        Today's partial row counts exactly the lead responses submitted so far.
        """
        rows: list[LinkedInCreativeDay] = []
        days = self.core.days(start, end, None)
        if days and days[-1] == self.core.today(None):
            self.ensure_leads_through(days[-1])
        now = self.core.now()
        for day in days:
            fraction = self.core.fraction(day, None)
            for key, spec in self.creatives.items():
                campaign = self.campaigns[spec.campaign_key]
                if campaign.account_id != account_id:
                    continue
                full = self.creative_full(key, day)
                if full.is_zero:
                    continue
                metrics = full
                if fraction < 1:
                    metrics = _partial(full, fraction)
                    leads = sum(
                        1
                        for response in self.responses_by_creative_day.get((key, day), ())
                        if not response.test_lead and response.submitted_at <= now
                    )
                    opens = max(metrics.form_opens, leads)
                    clicks = max(metrics.clicks, opens)
                    metrics = metrics.with_(
                        leads=leads,
                        form_opens=opens,
                        clicks=clicks,
                        impressions=max(metrics.impressions, clicks),
                    )
                    if metrics.is_zero:
                        continue
                rows.append(
                    LinkedInCreativeDay(
                        creative_id=self.creative_ids[key],
                        campaign_id=self.campaign_ids[spec.campaign_key],
                        account_id=account_id,
                        date=day,
                        cost=metrics.cost,
                        impressions=metrics.impressions,
                        clicks=metrics.clicks,
                        landing_page_clicks=metrics.landing_page_clicks,
                        form_opens=metrics.form_opens,
                        leads=metrics.leads,
                        website_conversions=metrics.website_conversions,
                    )
                )
        rows.sort(key=lambda row: (row.date, row.creative_id))
        return tuple(rows)

    # -- demographic segments ----------------------------------------------------------------------

    def _distribution(
        self, campaign_key: str, pivot: str
    ) -> tuple[list[tuple[str, float, float]], bool]:
        """``[(value, impression share, lead weight)]`` and whether leads are available.

        The lead weight is ``share / cpl_factor``; the catalogue values not listed explicitly
        share the remaining impressions with a factor that keeps the campaign's lead total.
        """
        segment = self.segments.get((campaign_key, pivot))
        defaults = [(value, float(share)) for value, share in _DEFAULT_SEGMENT_SHARES[pivot]]
        if segment is None or not segment.values:
            available = True if segment is None else segment.leads_available
            return [(value, share, share) for value, share in defaults], available
        explicit = [
            (
                value.value,
                float(value.impression_share),
                float(value.impression_share / value.cpl_factor),
            )
            for value in segment.values
        ]
        listed = {value for value, _share, _weight in explicit}
        rest = [(value, share) for value, share in defaults if value not in listed]
        rest_share = max(0.0, 1.0 - sum(share for _value, share, _weight in explicit))
        rest_leads = max(0.0, 1.0 - sum(weight for _value, _share, weight in explicit))
        base = sum(share for _value, share in rest)
        result = list(explicit)
        if base > 0.0 and rest_share > 0.0:
            for value, share in rest:
                portion = rest_share * share / base
                result.append((value, portion, rest_leads * share / base))
        return result, segment.leads_available

    def segment_records(
        self,
        account_id: int,
        pivot: str,
        start: date,
        end: date,
        campaign_ids: Iterable[int] | None,
    ) -> tuple[LinkedInSegmentRecord, ...]:
        """``ALL``-granularity rows per (campaign, demographic value) for the exact range."""
        _check_choice("pivot", pivot, SEGMENT_PIVOTS)
        wanted = None if campaign_ids is None else set(campaign_ids)
        days = self.core.days(start, end, None)
        records: list[LinkedInSegmentRecord] = []
        for campaign_key, campaign in self.campaigns.items():
            campaign_id = self.campaign_ids[campaign_key]
            if campaign.account_id != account_id or (
                wanted is not None and campaign_id not in wanted
            ):
                continue
            totals = _ZERO_DAY
            for day in days:
                for key in self.campaign_creatives[campaign_key]:
                    metrics = self.creative_full(key, day)
                    if not metrics.is_zero:
                        totals = _plus(totals, metrics)
            if totals.impressions == 0:
                continue
            distribution, available = self._distribution(campaign_key, pivot)
            rows: list[LinkedInSegmentRecord] = []
            for value, share, weight in distribution:
                impressions = int(round(totals.impressions * share, -1))
                if impressions < self.spec.segment_min_impressions:
                    continue
                rows.append(
                    LinkedInSegmentRecord(
                        account_id=account_id,
                        campaign_id=campaign_id,
                        pivot=pivot,
                        value=value,
                        impressions=impressions,
                        clicks=round(totals.clicks * share),
                        cost=_money(totals.cost * Decimal(share)),
                        leads=round(totals.leads * weight) if available else None,
                        website_conversions=round(totals.website_conversions * share)
                        if available
                        else None,
                    )
                )
            rows.sort(key=lambda row: (-(row.impressions or 0), row.value))
            records.extend(rows[: self.spec.segment_top_values])
        return tuple(records)

    # -- lead forms --------------------------------------------------------------------------------

    def _active_from(self, version: FormVersionSpec) -> datetime:
        if version.active_from_offset is None:
            return self._history_instant
        return _midnight(self.core.anchor + timedelta(days=version.active_from_offset))

    def version_record(self, form_id: int, version: FormVersionSpec) -> LinkedInFormVersionRecord:
        """The typed record of one form version."""
        return LinkedInFormVersionRecord(
            form_id=form_id,
            version=version.version,
            questions=tuple(
                LinkedInQuestionRecord(
                    question_id=question.question_id,
                    name=question.name,
                    predefined_field=question.predefined_field,
                    text=question.text or question.name,
                )
                for question in version.questions
            ),
            active_from=self._active_from(version),
        )

    def version_at(self, form_id: int, instant: datetime) -> FormVersionSpec:
        """The version of a form that a response submitted at ``instant`` uses."""
        form = self.forms[form_id]
        chosen = form.versions[0]
        for version in form.versions:
            if self._active_from(version) <= instant:
                chosen = version
        return chosen

    def form_records(self, account_id: int, at: datetime) -> tuple[LinkedInLeadFormRecord, ...]:
        """Lead forms owned by an account at ``at``, each at its current version, in id order."""
        records: list[LinkedInLeadFormRecord] = []
        for index, (form_id, form) in enumerate(sorted(self.forms.items())):
            if form.account_id != account_id:
                continue
            current = self.version_at(form_id, at)
            record = self.version_record(form_id, current)
            created = self.created_at(300 + index)
            records.append(
                LinkedInLeadFormRecord(
                    id=form_id,
                    account_id=account_id,
                    name=form.name,
                    state="PUBLISHED",
                    current_version=current.version,
                    questions=record.questions,
                    created_at=created,
                    last_modified_at=max(created, record.active_from),
                )
            )
        return tuple(records)

    # -- lead responses and people -----------------------------------------------------------------

    def _raw_leads(self, key: str, day: date) -> list[_RawLead]:
        """Every response a creative generated on UTC ``day`` (full day, test leads included)."""
        metrics = self.creative_full(key, day)
        if metrics.leads == 0:
            return []
        share = float(self.spec.test_lead_share)
        tests = 0
        if share > 0.0:
            tests = _count(
                metrics.leads * share / (1.0 - share),
                self.core.day_rng("linkedin:test-leads", key, day).random(),
            )
        hide_share = float(self.spec.unknown_creative_share)
        start = _midnight(day)
        leads: list[_RawLead] = []
        for index in range(metrics.leads + tests):
            label = f"{key}:{day.isoformat()}:{index}"
            rng = _rng(self.core.seed, "lead", label)
            submitted = start + timedelta(
                seconds=rng.randrange(86_400), milliseconds=rng.randrange(1000)
            )
            test = index >= metrics.leads
            leads.append(
                _RawLead(
                    id=_response_id(_sha256(f"{self.core.seed}:lead-id:{label}")),
                    creative_key=key,
                    submitted_at=submitted,
                    test_lead=test,
                    hide_creative=not test and rng.random() < hide_share,
                    first_name=rng.choice(_FIRST_NAMES),
                    last_name=rng.choice(_LAST_NAMES),
                    company=rng.choice(_COMPANIES),
                    title=rng.choice(_JOB_TITLES),
                    country=rng.choice(_COUNTRIES),
                    challenge=rng.choice(_CHALLENGES),
                )
            )
        return leads

    def ensure_leads_through(self, last: date) -> None:
        """Generate lead responses and people for every UTC day up to ``last``, in order."""
        last = min(last, self.core.today(None))
        with self._lock:
            day = (
                self.core.history_start
                if self._leads_through is None
                else self._leads_through + _DAY
            )
            while day <= last:
                raw = [lead for key in self.lead_creatives for lead in self._raw_leads(key, day)]
                raw.sort(key=lambda lead: (lead.submitted_at, lead.id))
                responses = [self._finish(lead) for lead in raw]
                self.responses_by_day[day] = responses
                for response in responses:
                    self.responses_by_id[response.id] = response
                    creative_key = self.creative_by_id[response.source_creative_id]
                    self.responses_by_creative_day.setdefault((creative_key, day), []).append(
                        response
                    )
                self._leads_through = day
                day += _DAY

    def _person_for(self, lead: _RawLead) -> _Person:
        """Assign the person who submitted ``lead``: a new one, or a repeat of an earlier one."""
        rng = _rng(self.core.seed, "person", lead.id)
        if self.persons and rng.random() < self._repeat_share:
            person = self.persons[int(rng.random() * len(self.persons))]
            person.touches.append(lead.submitted_at)
            return person
        suffix = _sha256(f"{self.core.seed}:person:{lead.id}")[:8]
        email = f"{lead.first_name}.{lead.last_name}.{suffix}@{SIM_EMAIL_DOMAIN}".lower()
        person = _Person(
            index=len(self.persons),
            key=lead.id,
            email=email,
            email_sha256=_sha256(_normalise_email(email)),
            first_name=lead.first_name,
            last_name=lead.last_name,
            first_touch=lead.submitted_at,
            touches=[lead.submitted_at],
        )
        self.persons.append(person)
        self.person_by_hash[person.email_sha256] = person
        return person

    def _finish(self, lead: _RawLead) -> LinkedInLeadResponse:
        """Build the typed response of a raw lead, with its person, version and answers."""
        spec = self.creatives[lead.creative_key]
        form_id = cast(int, spec.lead_form_id)
        campaign = self.campaigns[spec.campaign_key]
        version = self.version_at(form_id, lead.submitted_at)
        if lead.test_lead:
            email = f"test-lead-{lead.id[:8]}@{SIM_EMAIL_DOMAIN}"
            person_index: int | None = None
            first, last = "Test", "Lead"
        else:
            person = self._person_for(lead)
            email, person_index = person.email, person.index
            first, last = person.first_name, person.last_name
        answers: list[LinkedInLeadAnswer] = []
        for question in version.questions:
            field_name = question.predefined_field
            if field_name in ("EMAIL", "WORK_EMAIL"):
                answer = email
            elif field_name == "FIRST_NAME":
                answer = first
            elif field_name == "LAST_NAME":
                answer = last
            elif field_name == "COMPANY_NAME":
                answer = lead.company
            elif field_name == "JOB_TITLE":
                answer = lead.title
            elif field_name == "COUNTRY":
                answer = lead.country
            else:
                answer = lead.challenge
            answers.append(LinkedInLeadAnswer(question_id=question.question_id, answer=answer))
        creative_id = self.creative_ids[lead.creative_key]
        return LinkedInLeadResponse(
            id=lead.id,
            account_id=campaign.account_id,
            campaign_id=self.campaign_ids[spec.campaign_key],
            creative_id=None if lead.hide_creative else creative_id,
            source_creative_id=creative_id,
            form_id=form_id,
            form_version=version.version,
            submitted_at=lead.submitted_at,
            test_lead=lead.test_lead,
            email=email,
            answers=tuple(answers),
            person_index=person_index,
        )

    def responses(
        self, account_id: int, start: datetime, end: datetime
    ) -> tuple[LinkedInLeadResponse, ...]:
        """Responses of an account submitted in ``[start, end)`` and by now, test leads included."""
        now = self.core.now()
        stop = min(end, now + timedelta(microseconds=1))
        if stop <= start:
            return ()
        self.ensure_leads_through((stop - timedelta(microseconds=1)).date())
        first = max(start.date(), self.core.history_start)
        found: list[LinkedInLeadResponse] = []
        day = first
        while day <= (stop - timedelta(microseconds=1)).date():
            for response in self.responses_by_day.get(day, ()):
                if response.account_id == account_id and start <= response.submitted_at < stop:
                    found.append(response)
            day += _DAY
        return tuple(found)

    def response(self, response_id: str) -> LinkedInLeadResponse | None:
        """One response by plain id, when it has been submitted by now."""
        self.ensure_leads_through(self.core.today(None))
        found = self.responses_by_id.get(response_id)
        if found is None or found.submitted_at > self.core.now():
            return None
        return found


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ContactTruth:
    """Everything that will ever happen to one contact (internal); reads filter by now."""

    id: str
    email: str | None
    email_sha256: str | None
    first_name: str
    last_name: str
    created_at: datetime
    mql_at: datetime | None
    ql_at: datetime | None
    meeting_ids: tuple[str, ...]
    deal_ids: tuple[str, ...]
    google_campaign_ref: str | None
    google_touch_at: datetime | None


@dataclass(frozen=True, slots=True)
class _MeetingTruth:
    """One meeting (internal)."""

    id: str
    title: str
    created_at: datetime
    start_at: datetime
    source: str
    canceled: bool
    contact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DealTruth:
    """One deal (internal)."""

    id: str
    name: str
    created_at: datetime
    stage: str
    amount: Decimal
    contact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _MeetingDraw:
    created_at: datetime
    start_at: datetime
    source: str
    canceled: bool
    shared: bool


@dataclass(frozen=True, slots=True)
class _FunnelDraw:
    created_at: datetime
    mql_at: datetime | None
    ql_at: datetime | None
    meetings: tuple[_MeetingDraw, ...]
    deals: tuple[tuple[datetime, str, Decimal], ...]


_DEAL_STAGE_WEIGHTS: Final = (0.22, 0.2, 0.15, 0.12, 0.1, 0.12, 0.09)


def _comparable(value: PropertyValue) -> int | Decimal | str | None:
    """A property value in the form CRM Search compares: epoch ms for dates and datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _epoch_ms(value)
    if isinstance(value, date):
        return _epoch_ms(_midnight(value))
    return value


def _coerce(
    raw: int | str | Decimal | None, like: int | Decimal | str
) -> int | Decimal | str | None:
    """Coerce a filter operand to the type of the property value it is compared with."""
    if raw is None:
        return None
    try:
        if isinstance(like, int):
            return int(Decimal(str(raw)))
        if isinstance(like, Decimal):
            return Decimal(str(raw))
    except ArithmeticError:
        return None
    return str(raw)


def _filter_holds(actual: int | Decimal | str | None, flt: SearchFilter) -> bool:
    """Evaluate one CRM Search filter against a comparable property value."""
    operator = flt.operator
    if operator == "HAS_PROPERTY":
        return actual is not None
    if operator == "NOT_HAS_PROPERTY":
        return actual is None
    if actual is None:
        return False
    if operator == "IN":
        return str(actual) in flt.values
    low = _coerce(flt.value, actual)
    if low is None:
        return False
    left = cast(Any, actual)
    if operator == "EQ":
        return bool(left == low)
    if operator == "NEQ":
        return bool(left != low)
    if operator == "LT":
        return bool(left < low)
    if operator == "LTE":
        return bool(left <= low)
    if operator == "GT":
        return bool(left > low)
    if operator == "GTE":
        return bool(left >= low)
    high = _coerce(flt.high_value, actual)
    return high is not None and bool(low <= left <= high)


class _CrmModel:
    """The simulated HubSpot portal: stages, properties, contacts, meetings, deals, links.

    It answers for three populations: the simulated LinkedIn people (the companion CRM
    dataset when LinkedIn is simulated, or a simulated primary dataset), the Google-touch
    contacts of a configured Google CRM link, and seeded contacts that answer real lead emails
    by hash when LinkedIn is live (their plaintext is never seen or stored).
    """

    def __init__(
        self,
        core: _Core,
        spec: HubSpotSpec,
        mapping: HubSpotMapping,
        linkedin: _LinkedInModel | None,
        google: _GoogleModel | None,
        google_properties: tuple[str, str] | None,
    ) -> None:
        self.core = core
        self.spec = spec
        self.funnel = spec.funnel
        self.mapping = mapping
        self.linkedin = linkedin
        self.google = google
        self.google_properties = google_properties
        self.options, self.added_labels = self._build_options()
        self.option_order = {option.value: option.display_order for option in self.options}
        self.mql_target = self._target(mapping.mql)
        self.ql_target = self._target(mapping.ql)
        self.properties = self._build_properties()
        self._lock = threading.RLock()
        self._people_done = 0
        self._contacts: dict[str, _ContactTruth] = {}
        self._contact_by_hash: dict[str, str] = {}
        self._person_contacts: list[str] = []
        self._meetings: dict[str, _MeetingTruth] = {}
        self._deals: dict[str, _DealTruth] = {}
        self._meetings_of: dict[str, list[str]] = {}
        self._deals_of: dict[str, list[str]] = {}
        self._meeting_counter = 0
        self._deal_counter = 0
        self._hashed_cache: dict[
            int, tuple[_ContactTruth, tuple[_MeetingTruth, ...], tuple[_DealTruth, ...]]
        ] = {}
        self._google_built = False

    # -- stages and properties ---------------------------------------------------------------------

    def _build_options(self) -> tuple[tuple[PropertyOption, ...], tuple[str, ...]]:
        """The ``lifecyclestage`` options: defaults with the custom stages after ``lead``.

        Labels the mapping names but the portal lacks are added as custom stages unless the
        spec removes them deliberately (then the binding stays unresolved).

        Raises:
            ValueError: When a removed label is neither a stage nor a label the mapping names.
        """
        removed = set(self.spec.removed_stage_labels)
        mapping_labels: list[str] = []
        for binding in (self.mapping.mql, self.mapping.ql):
            if binding.mode == "stage_label":
                mapping_labels.extend(binding.stage_labels)
        known = {label for _value, label in DEFAULT_LIFECYCLE_STAGES}
        known |= {stage.label for stage in self.spec.custom_stages}
        for label in removed:
            _require(
                label in known or label in mapping_labels,
                f"removed stage label {label!r} is neither a portal stage nor a mapping label",
            )
        entries: list[tuple[str, str]] = []
        for value, label in DEFAULT_LIFECYCLE_STAGES[:2]:
            entries.append((value, label))
        entries.extend((stage.id, stage.label) for stage in self.spec.custom_stages)
        entries.extend(DEFAULT_LIFECYCLE_STAGES[2:])
        entries = [(value, label) for value, label in entries if label not in removed]
        labels = {label for _value, label in entries}
        used_ids = {stage.id for stage in self.spec.custom_stages}
        added: list[str] = []
        for binding in (self.mapping.mql, self.mapping.ql):
            if binding.mode != "stage_label" or any(
                label in labels for label in binding.stage_labels
            ):
                continue
            candidates = [label for label in binding.stage_labels if label not in removed]
            if not candidates:
                continue
            offset = 0
            while str(HUBSPOT_STAGE_IDS.at(offset)) in used_ids:
                offset += 1
            stage_id = str(HUBSPOT_STAGE_IDS.at(offset))
            used_ids.add(stage_id)
            entries.insert(len(entries) - 1, (stage_id, candidates[0]))
            labels.add(candidates[0])
            added.append(candidates[0])
        options = tuple(
            PropertyOption(label=label, value=value, display_order=order)
            for order, (value, label) in enumerate(entries)
        )
        return options, tuple(added)

    def _target(self, binding: StageBinding) -> tuple[str, str] | None:
        """Where a binding's event is recorded: ``("stage", value)``, ``("property", name)``."""
        if binding.mode == "property":
            return ("property", cast(str, binding.property))
        by_label = {option.label: option.value for option in self.options}
        for label in binding.stage_labels:
            value = by_label.get(label)
            if value is not None:
                return ("stage", value)
        return None

    def _build_properties(self) -> Mapping[tuple[str, str], PropertyRecord]:
        """Every property definition the portal exposes, keyed ``(object type, name)``."""

        def text(obj: str, name: str, label: str) -> PropertyRecord:
            return PropertyRecord(obj, name, label, "string", "text")

        def instant(obj: str, name: str, label: str) -> PropertyRecord:
            return PropertyRecord(obj, name, label, "datetime", "date")

        def number(obj: str, name: str, label: str) -> PropertyRecord:
            return PropertyRecord(obj, name, label, "number", "number")

        def enum(
            obj: str, name: str, label: str, values: Sequence[tuple[str, str]]
        ) -> PropertyRecord:
            options = tuple(
                PropertyOption(label=option_label, value=value, display_order=order)
                for order, (value, option_label) in enumerate(values)
            )
            return PropertyRecord(obj, name, label, "enumeration", "select", options)

        records: list[PropertyRecord] = [
            text("contacts", "email", "Email"),
            text("contacts", "firstname", "First Name"),
            text("contacts", "lastname", "Last Name"),
            instant("contacts", "createdate", "Create Date"),
            instant("contacts", "lastmodifieddate", "Last Modified Date"),
            number("contacts", "hs_object_id", "Record ID"),
            PropertyRecord(
                "contacts",
                "lifecyclestage",
                "Lifecycle Stage",
                "enumeration",
                "radio",
                self.options,
            ),
        ]
        for option in self.options:
            records.append(
                instant(
                    "contacts",
                    f"hs_v2_date_entered_{option.value}",
                    f"Date entered '{option.label} (Lifecycle Stage Pipeline)'",
                )
            )
        for target in (self.mql_target, self.ql_target):
            if target is not None and target[0] == "property":
                records.append(PropertyRecord("contacts", target[1], target[1], "date", "date"))
        if self.google_properties is not None:
            campaign_property, touch_property = self.google_properties
            records.append(text("contacts", campaign_property, "Google Ads campaign"))
            records.append(instant("contacts", touch_property, "Google Ads touch date"))
        records += [
            number("meetings", "hs_object_id", "Record ID"),
            instant("meetings", "hs_createdate", "Create Date"),
            instant("meetings", "hs_lastmodifieddate", "Last Modified Date"),
            instant("meetings", "hs_timestamp", "Activity date"),
            text("meetings", "hs_meeting_title", "Meeting name"),
            instant("meetings", "hs_meeting_start_time", "Meeting start time"),
            instant("meetings", "hs_meeting_end_time", "Meeting end time"),
            enum(
                "meetings",
                "hs_meeting_outcome",
                "Meeting outcome",
                [(value, value.title()) for value in _MEETING_OUTCOMES],
            ),
            enum(
                "meetings",
                "hs_meeting_source",
                "Meeting source",
                [(value, value.title()) for value in _MEETING_SOURCES],
            ),
            number("deals", "hs_object_id", "Record ID"),
            text("deals", "dealname", "Deal Name"),
            instant("deals", "createdate", "Create Date"),
            instant("deals", "hs_lastmodifieddate", "Last Modified Date"),
            instant("deals", "closedate", "Close Date"),
            enum("deals", "dealstage", "Deal Stage", [(value, value) for value in _DEAL_STAGES]),
            enum("deals", "pipeline", "Pipeline", [("default", "Sales Pipeline")]),
            number("deals", "amount", "Amount"),
            enum(
                "deals",
                "deal_currency_code",
                "Currency",
                [(self.spec.deal_currency, self.spec.deal_currency)],
            ),
        ]
        return MappingProxyType({(record.object_type, record.name): record for record in records})

    # -- funnel draws -----------------------------------------------------------------------------

    def _span(self, rng: random.Random, window: tuple[int, int]) -> timedelta:
        return timedelta(days=rng.uniform(window[0], window[1]))

    def _source(self, rng: random.Random) -> str:
        weights = self.funnel.meeting_sources
        total = float(sum((weight for _source, weight in weights), Decimal(0)))
        pick = rng.random() * total
        for source, weight in weights:
            pick -= float(weight)
            if pick < 0.0:
                return source
        return weights[-1][0]

    def _draw(self, rng: random.Random, touch: datetime, *, max_meetings: int = 2) -> _FunnelDraw:
        """Draw one contact's CRM history relative to its first touch."""
        funnel = self.funnel
        pre = rng.random() < float(funnel.pre_touch_share)
        late = rng.random() < float(funnel.late_contact_share)
        mql = rng.random() < float(funnel.mql_share)
        ql = rng.random() < float(funnel.ql_share)
        skipped = ql and rng.random() < float(funnel.skipped_stage_share)
        if pre:
            created = touch - timedelta(days=rng.uniform(20, 60))
            mql_at = touch - timedelta(days=rng.uniform(5, 15)) if mql else None
            ql_at = touch - timedelta(days=rng.uniform(1, 4)) if ql else None
        else:
            delay = funnel.late_contact_delay if late else timedelta(minutes=rng.uniform(3, 90))
            created = touch + delay
            mql_at = max(touch + self._span(rng, funnel.mql_delay_days), created) if mql else None
            ql_at = max(touch + self._span(rng, funnel.ql_delay_days), created) if ql else None
        if mql_at is not None and ql_at is not None and ql_at < mql_at:
            mql_at, ql_at = ql_at, mql_at
        if skipped:
            mql_at = None
        meetings: list[_MeetingDraw] = []
        if rng.random() < float(funnel.meeting_share):
            count = min(max_meetings, 2 if rng.random() < 0.2 else 1)
            for _index in range(count):
                source = self._source(rng)
                canceled = rng.random() < float(funnel.canceled_share)
                after_start = rng.random() < float(funnel.meeting_created_after_start_share)
                shared = rng.random() < float(funnel.two_contact_meeting_share)
                if pre:
                    made = touch - timedelta(days=rng.uniform(1, 15))
                    start = made + timedelta(days=rng.uniform(1, 7))
                elif after_start:
                    start = touch - timedelta(hours=rng.uniform(24, 72))
                    made = touch + timedelta(hours=rng.uniform(1, 48))
                else:
                    made = touch + self._span(rng, funnel.meeting_delay_days)
                    start = made + timedelta(days=rng.uniform(1, 7))
                made = max(made, created + timedelta(minutes=1))
                start = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
                meetings.append(_MeetingDraw(made, start, source, canceled, shared))
        deals: list[tuple[datetime, str, Decimal]] = []
        if rng.random() < float(funnel.deal_share):
            made = max(
                touch + self._span(rng, funnel.deal_delay_days), created + timedelta(hours=1)
            )
            deals.append((made, self._stage(rng), Decimal(rng.randrange(50, 600) * 100)))
        if rng.random() < float(funnel.pre_touch_deal_share):
            made = touch - timedelta(days=rng.uniform(5, 40))
            created = min(created, made - _DAY)
            deals.append((made, self._stage(rng), Decimal(rng.randrange(50, 600) * 100)))
        return _FunnelDraw(created, mql_at, ql_at, tuple(meetings), tuple(deals))

    def _stage(self, rng: random.Random) -> str:
        return rng.choices(_DEAL_STAGES, weights=_DEAL_STAGE_WEIGHTS, k=1)[0]

    # -- populations -------------------------------------------------------------------------------

    def _ensure_people(self) -> None:
        """Build the contacts of every simulated LinkedIn person generated so far."""
        if self.linkedin is not None:
            self.linkedin.ensure_leads_through(self.core.today(None))
        with self._lock:
            if self.linkedin is not None:
                persons = self.linkedin.persons
                while self._people_done < len(persons):
                    self._build_person(persons[self._people_done])
                    self._people_done += 1
            self._ensure_google()

    def _build_person(self, person: _Person) -> None:
        rng = _rng(self.core.seed, "crm", person.key)
        if rng.random() >= float(self.funnel.match_share):
            return
        contact_id = str(HUBSPOT_CONTACT_IDS.at(person.index))
        draw = self._draw(rng, person.first_touch)
        self._register(
            contact_id,
            draw,
            rng,
            person.email,
            person.email_sha256,
            person.first_name,
            person.last_name,
            None,
            None,
            google=False,
        )
        self._person_contacts.append(contact_id)

    def _register(
        self,
        contact_id: str,
        draw: _FunnelDraw,
        rng: random.Random,
        email: str,
        email_sha256: str,
        first_name: str,
        last_name: str,
        google_ref: str | None,
        google_touch: datetime | None,
        *,
        google: bool,
    ) -> None:
        """Allocate a contact's meetings and deals and index everything."""
        meeting_ids: list[str] = []
        for draw_meeting in draw.meetings:
            if google:
                meeting_id = str(HUBSPOT_MEETING_IDS.at(_GOOGLE_CRM_BLOCK + self._meeting_counter))
            else:
                meeting_id = str(HUBSPOT_MEETING_IDS.at(self._meeting_counter))
            self._meeting_counter += 1
            contacts = [contact_id]
            if draw_meeting.shared and self._person_contacts and not google:
                partner = self._person_contacts[int(rng.random() * len(self._person_contacts))]
                if partner != contact_id:
                    contacts.append(partner)
            meeting = _MeetingTruth(
                id=meeting_id,
                title=f"Intro call with {first_name} {last_name}",
                created_at=draw_meeting.created_at,
                start_at=draw_meeting.start_at,
                source=draw_meeting.source,
                canceled=draw_meeting.canceled,
                contact_ids=tuple(sorted(contacts)),
            )
            self._meetings[meeting_id] = meeting
            for member in meeting.contact_ids:
                self._meetings_of.setdefault(member, []).append(meeting_id)
            meeting_ids.append(meeting_id)
        deal_ids: list[str] = []
        for made, stage, amount in draw.deals:
            if google:
                deal_id = str(HUBSPOT_DEAL_IDS.at(_GOOGLE_CRM_BLOCK + self._deal_counter))
            else:
                deal_id = str(HUBSPOT_DEAL_IDS.at(self._deal_counter))
            self._deal_counter += 1
            self._deals[deal_id] = _DealTruth(
                id=deal_id,
                name=f"{last_name} - Analytics platform",
                created_at=made,
                stage=stage,
                amount=amount,
                contact_ids=(contact_id,),
            )
            self._deals_of.setdefault(contact_id, []).append(deal_id)
            deal_ids.append(deal_id)
        self._contacts[contact_id] = _ContactTruth(
            id=contact_id,
            email=email,
            email_sha256=email_sha256,
            first_name=first_name,
            last_name=last_name,
            created_at=draw.created_at,
            mql_at=draw.mql_at,
            ql_at=draw.ql_at,
            meeting_ids=tuple(meeting_ids),
            deal_ids=tuple(deal_ids),
            google_campaign_ref=google_ref,
            google_touch_at=google_touch,
        )
        self._contact_by_hash[email_sha256] = contact_id

    def _ensure_google(self) -> None:
        """Build the Google-touch contacts once, when the Google CRM link is configured."""
        if self._google_built:
            return
        self._google_built = True
        if self.google_properties is None or self.google is None:
            return
        campaigns = [
            self.google.campaign_ids[key]
            for key, spec in self.google.campaigns.items()
            if spec.channel == "SEARCH"
        ]
        if not campaigns:
            return
        start = _midnight(self.core.history_start)
        span = float(self.core.spec.history_days)
        saved_meetings, saved_deals = self._meeting_counter, self._deal_counter
        self._meeting_counter = self._deal_counter = 0
        for index in range(self.spec.google_touch_contacts):
            rng = _rng(self.core.seed, "google-crm", str(index))
            touch = start + timedelta(days=rng.uniform(0.0, span))
            campaign = campaigns[int(rng.random() * len(campaigns))]
            first, last = rng.choice(_FIRST_NAMES), rng.choice(_LAST_NAMES)
            email = f"google-contact-{index:05d}@{SIM_EMAIL_DOMAIN}"
            draw = self._draw(rng, touch)
            self._register(
                str(HUBSPOT_CONTACT_IDS.at(_GOOGLE_CRM_BLOCK + index)),
                draw,
                rng,
                email,
                _sha256(email),
                first,
                last,
                str(campaign),
                touch,
                google=True,
            )
        self._meeting_counter, self._deal_counter = saved_meetings, saved_deals

    @cached_property
    def _orphans(self) -> tuple[_DealTruth, ...]:
        """Deals with no contact, created over the 90 days before the anchor."""
        deals: list[_DealTruth] = []
        anchor = _midnight(self.core.anchor)
        for index in range(self.spec.orphan_deals):
            rng = _rng(self.core.seed, "orphan-deal", str(index))
            deals.append(
                _DealTruth(
                    id=str(HUBSPOT_DEAL_IDS.at(_ORPHAN_DEAL_BLOCK + index)),
                    name=f"Inbound deal {index + 1:03d}",
                    created_at=anchor - timedelta(days=rng.uniform(1.0, 90.0)),
                    stage=self._stage(rng),
                    amount=Decimal(rng.randrange(50, 600) * 100),
                    contact_ids=(),
                )
            )
        return tuple(deals)

    def _bulk_deal(self, index: int) -> _DealTruth | None:
        """The ``index``-th bulk deal, generated without random draws."""
        bulk = self.spec.bulk_deals
        if bulk is None or not 0 <= index < bulk.count:
            return None
        if index < bulk.peak_hour_count:
            hour = bulk.peak_hour
        else:
            others = [hour for hour in range(24) if hour != bulk.peak_hour]
            hour = others[(index - bulk.peak_hour_count) % len(others)]
        start = _midnight(self.core.anchor + timedelta(days=bulk.day_offset))
        return _DealTruth(
            id=str(HUBSPOT_DEAL_IDS.at(_BULK_DEAL_BLOCK + index)),
            name=f"Bulk deal {index + 1:05d}",
            created_at=start + timedelta(hours=hour, seconds=(index * 7919) % 3600),
            stage="appointmentscheduled",
            amount=Decimal(1000),
            contact_ids=(),
        )

    @cached_property
    def _bulk_deals(self) -> tuple[_DealTruth, ...]:
        bulk = self.spec.bulk_deals
        if bulk is None:
            return ()
        return tuple(cast(_DealTruth, self._bulk_deal(index)) for index in range(bulk.count))

    # -- seeded contacts for real lead emails ------------------------------------------------------

    def _hashed(
        self, offset: int
    ) -> tuple[_ContactTruth, tuple[_MeetingTruth, ...], tuple[_DealTruth, ...]]:
        """The seeded contact at ``offset`` of ``HUBSPOT_HASHED_CONTACT_IDS`` and its objects.

        Everything is derived from the contact id (itself derived from the email hash), so a
        later by-id read, even in a new process, finds the same contact.
        """
        cached = self._hashed_cache.get(offset)
        if cached is not None:
            return cached
        contact_id = str(HUBSPOT_HASHED_CONTACT_IDS.at(offset))
        rng = _rng(self.core.seed, "hashed", contact_id)
        touch = _midnight(self.core.anchor) - timedelta(days=rng.uniform(0.0, 120.0))
        draw = self._draw(rng, touch, max_meetings=_HASHED_OBJECTS_PER_CONTACT)
        first, last = rng.choice(_FIRST_NAMES), rng.choice(_LAST_NAMES)
        base = _HASHED_OBJECT_BLOCK + offset * _HASHED_OBJECTS_PER_CONTACT
        meetings = tuple(
            _MeetingTruth(
                id=str(HUBSPOT_MEETING_IDS.at(base + index)),
                title=f"Intro call with {first} {last}",
                created_at=meeting.created_at,
                start_at=meeting.start_at,
                source=meeting.source,
                canceled=meeting.canceled,
                contact_ids=(contact_id,),
            )
            for index, meeting in enumerate(draw.meetings[:_HASHED_OBJECTS_PER_CONTACT])
        )
        deals = tuple(
            _DealTruth(
                id=str(HUBSPOT_DEAL_IDS.at(base + index)),
                name=f"{last} - Analytics platform",
                created_at=made,
                stage=stage,
                amount=amount,
                contact_ids=(contact_id,),
            )
            for index, (made, stage, amount) in enumerate(draw.deals[:_HASHED_OBJECTS_PER_CONTACT])
        )
        truth = _ContactTruth(
            id=contact_id,
            email=None,
            email_sha256=None,
            first_name=first,
            last_name=last,
            created_at=draw.created_at,
            mql_at=draw.mql_at,
            ql_at=draw.ql_at,
            meeting_ids=tuple(meeting.id for meeting in meetings),
            deal_ids=tuple(deal.id for deal in deals),
            google_campaign_ref=None,
            google_touch_at=None,
        )
        entry = (truth, meetings, deals)
        with self._lock:
            self._hashed_cache[offset] = entry
        return entry

    def hashed_offset(self, sha256_hex: str) -> int | None:
        """The seeded-contact offset answering an email hash, or ``None`` (no contact)."""
        digest = sha256_hex.strip().lower()
        _require(
            len(digest) == 64 and all(char in "0123456789abcdef" for char in digest),
            "an email hash must be a SHA-256 hex digest",
        )
        if int(digest[:8], 16) % 10_000 >= self.spec.live_lead_match_share * 10_000:
            return None
        return int(digest[8:24], 16) % _HASHED_CONTACT_SPAN

    def _hashed_object(self, family: IdRange, object_id: str) -> tuple[int, int] | None:
        """Decode a seeded contact's meeting or deal id into ``(contact offset, index)``."""
        if not family.contains(object_id):
            return None
        relative = int(object_id) - family.start - _HASHED_OBJECT_BLOCK
        if relative < 0:
            return None
        return relative // _HASHED_OBJECTS_PER_CONTACT, relative % _HASHED_OBJECTS_PER_CONTACT

    # -- lookups -----------------------------------------------------------------------------------

    def contact_truth(self, contact_id: str) -> _ContactTruth | None:
        """Any contact by id, whether or not it exists yet."""
        if HUBSPOT_HASHED_CONTACT_IDS.contains(contact_id):
            offset = int(contact_id) - HUBSPOT_HASHED_CONTACT_IDS.start
            return self._hashed(offset)[0] if offset < _HASHED_CONTACT_SPAN else None
        self._ensure_people()
        return self._contacts.get(contact_id)

    def meeting_truth(self, meeting_id: str) -> _MeetingTruth | None:
        """Any meeting by id."""
        decoded = self._hashed_object(HUBSPOT_MEETING_IDS, meeting_id)
        if decoded is not None:
            offset, index = decoded
            meetings = self._hashed(offset)[1] if offset < _HASHED_CONTACT_SPAN else ()
            return meetings[index] if index < len(meetings) else None
        self._ensure_people()
        return self._meetings.get(meeting_id)

    def deal_truth(self, deal_id: str) -> _DealTruth | None:
        """Any deal by id."""
        decoded = self._hashed_object(HUBSPOT_DEAL_IDS, deal_id)
        if decoded is not None:
            offset, index = decoded
            deals = self._hashed(offset)[2] if offset < _HASHED_CONTACT_SPAN else ()
            return deals[index] if index < len(deals) else None
        if not HUBSPOT_DEAL_IDS.contains(deal_id):
            return None
        relative = int(deal_id) - HUBSPOT_DEAL_IDS.start
        if relative >= _BULK_DEAL_BLOCK:
            return self._bulk_deal(relative - _BULK_DEAL_BLOCK)
        if relative >= _ORPHAN_DEAL_BLOCK:
            index = relative - _ORPHAN_DEAL_BLOCK
            orphans = self._orphans
            return orphans[index] if index < len(orphans) else None
        self._ensure_people()
        return self._deals.get(deal_id)

    def contact_for_hash(self, sha256_hex: str) -> _ContactTruth | None:
        """The simulated person's (or Google-touch) contact whose email hashes to the value."""
        self._ensure_people()
        contact_id = self._contact_by_hash.get(sha256_hex.strip().lower())
        return None if contact_id is None else self._contacts.get(contact_id)

    # -- records as of now -------------------------------------------------------------------------

    def contact_record(
        self, truth: _ContactTruth, *, email_sha256: str | None = None
    ) -> ContactRecord | None:
        """The contact as of now, or ``None`` before it is created."""
        now = self.core.now()
        if truth.created_at > now:
            return None
        entered: dict[str, datetime] = {"lead": truth.created_at}
        bound: dict[str, date] = {}
        for target, instant in ((self.mql_target, truth.mql_at), (self.ql_target, truth.ql_at)):
            if target is None or instant is None or instant > now:
                continue
            kind, name = target
            if kind == "stage":
                previous = entered.get(name)
                entered[name] = instant if previous is None else min(previous, instant)
            else:
                day = instant.date()
                previous_day = bound.get(name)
                bound[name] = day if previous_day is None else min(previous_day, day)
        entered = {
            value: instant for value, instant in entered.items() if value in self.option_order
        }
        stage = max(entered, key=lambda value: self.option_order[value]) if entered else "lead"
        events = [truth.created_at, *entered.values()]
        events += [_midnight(day) for day in bound.values()]
        updated = max(instant for instant in events if instant <= now)
        return ContactRecord(
            id=truth.id,
            email=truth.email,
            email_sha256=email_sha256 if email_sha256 is not None else truth.email_sha256,
            first_name=truth.first_name,
            last_name=truth.last_name,
            created_at=truth.created_at,
            updated_at=updated,
            lifecycle_stage=stage,
            stage_entered=MappingProxyType(dict(sorted(entered.items(), key=lambda item: item[1]))),
            bound_dates=MappingProxyType(dict(sorted(bound.items()))),
            google_campaign_ref=truth.google_campaign_ref,
            google_touch_at=truth.google_touch_at,
        )

    def meeting_record(self, truth: _MeetingTruth) -> MeetingRecord | None:
        """The meeting as of now, or ``None`` before it is created."""
        now = self.core.now()
        if truth.created_at > now:
            return None
        end = truth.start_at + timedelta(minutes=30)
        if truth.canceled:
            outcome = "CANCELED"
        elif truth.start_at <= now:
            outcome = "COMPLETED"
        else:
            outcome = "SCHEDULED"
        updated = max(truth.created_at, end) if end <= now else truth.created_at
        contacts = tuple(
            contact_id
            for contact_id in truth.contact_ids
            if (contact := self.contact_truth(contact_id)) is not None and contact.created_at <= now
        )
        return MeetingRecord(
            id=truth.id,
            title=truth.title,
            created_at=truth.created_at,
            updated_at=updated,
            timestamp=truth.start_at,
            start_at=truth.start_at,
            end_at=end,
            outcome=outcome,
            source=truth.source,
            contact_ids=contacts,
        )

    def deal_record(self, truth: _DealTruth) -> DealRecord | None:
        """The deal as of now, or ``None`` before it is created."""
        now = self.core.now()
        if truth.created_at > now:
            return None
        touched = truth.created_at + _DAY
        contacts = tuple(
            contact_id
            for contact_id in truth.contact_ids
            if (contact := self.contact_truth(contact_id)) is not None and contact.created_at <= now
        )
        return DealRecord(
            id=truth.id,
            name=truth.name,
            created_at=truth.created_at,
            updated_at=touched if touched <= now else truth.created_at,
            stage=truth.stage,
            pipeline="default",
            amount=truth.amount,
            currency=self.spec.deal_currency,
            contact_ids=contacts,
        )

    def properties_of(self, record: HubSpotRecord) -> Mapping[str, PropertyValue]:
        """Every property of a record by HubSpot name, typed (``None`` = no value)."""
        values: dict[str, PropertyValue] = {"hs_object_id": record.id}
        if isinstance(record, ContactRecord):
            values.update(
                {
                    "email": record.email,
                    "firstname": record.first_name,
                    "lastname": record.last_name,
                    "createdate": record.created_at,
                    "lastmodifieddate": record.updated_at,
                    "lifecyclestage": record.lifecycle_stage,
                }
            )
            for option in self.options:
                values[f"hs_v2_date_entered_{option.value}"] = record.stage_entered.get(
                    option.value
                )
            for target in (self.mql_target, self.ql_target):
                if target is not None and target[0] == "property":
                    values[target[1]] = record.bound_dates.get(target[1])
            if self.google_properties is not None:
                campaign_property, touch_property = self.google_properties
                values[campaign_property] = record.google_campaign_ref
                values[touch_property] = record.google_touch_at
        elif isinstance(record, MeetingRecord):
            values.update(
                {
                    "hs_createdate": record.created_at,
                    "hs_lastmodifieddate": record.updated_at,
                    "hs_timestamp": record.timestamp,
                    "hs_meeting_title": record.title,
                    "hs_meeting_start_time": record.start_at,
                    "hs_meeting_end_time": record.end_at,
                    "hs_meeting_outcome": record.outcome,
                    "hs_meeting_source": record.source,
                }
            )
        else:
            values.update(
                {
                    "dealname": record.name,
                    "createdate": record.created_at,
                    "hs_lastmodifieddate": record.updated_at,
                    "closedate": None,
                    "dealstage": record.stage,
                    "pipeline": record.pipeline,
                    "amount": record.amount,
                    "deal_currency_code": record.currency,
                }
            )
        return MappingProxyType(values)

    # -- associations and search -------------------------------------------------------------------

    def associations(self, from_type: str, to_type: str, object_id: str) -> tuple[str, ...]:
        """Associated ids (existing now), ascending; empty when there are none.

        Raises:
            ValueError: For an unsupported pair of object types.
        """
        now = self.core.now()
        pair = (from_type, to_type)
        if pair in (("contacts", "meetings"), ("contacts", "deals")):
            contact = self.contact_truth(object_id)
            if contact is None or contact.created_at > now:
                return ()
            if pair == ("contacts", "meetings"):
                if HUBSPOT_HASHED_CONTACT_IDS.contains(object_id):
                    ids: Iterable[str] = contact.meeting_ids
                else:
                    ids = self._meetings_of.get(object_id, ())
                found = [
                    meeting_id
                    for meeting_id in ids
                    if (meeting := self.meeting_truth(meeting_id)) is not None
                    and meeting.created_at <= now
                ]
            else:
                ids = (
                    contact.deal_ids
                    if HUBSPOT_HASHED_CONTACT_IDS.contains(object_id)
                    else self._deals_of.get(object_id, ())
                )
                found = [
                    deal_id
                    for deal_id in ids
                    if (deal := self.deal_truth(deal_id)) is not None and deal.created_at <= now
                ]
            return tuple(sorted(set(found), key=int))
        if pair == ("meetings", "contacts"):
            meeting = self.meeting_truth(object_id)
            record = None if meeting is None else self.meeting_record(meeting)
            return () if record is None else tuple(sorted(record.contact_ids, key=int))
        if pair == ("deals", "contacts"):
            deal = self.deal_truth(object_id)
            deal_record = None if deal is None else self.deal_record(deal)
            return () if deal_record is None else tuple(sorted(deal_record.contact_ids, key=int))
        raise ValueError(f"unsupported association {from_type} -> {to_type}")

    def search(
        self,
        object_type: str,
        filters: Sequence[SearchFilter],
        sort_property: str | None,
        descending: bool,
    ) -> tuple[HubSpotRecord, ...]:
        """Every existing record of a type matching all ``filters``, sorted.

        Raises:
            ValueError: For an unknown object type.
        """
        self._ensure_people()
        records: list[HubSpotRecord] = []
        if object_type == "contacts":
            for truth in list(self._contacts.values()):
                contact = self.contact_record(truth)
                if contact is not None:
                    records.append(contact)
        elif object_type == "meetings":
            for meeting_truth in list(self._meetings.values()):
                meeting = self.meeting_record(meeting_truth)
                if meeting is not None:
                    records.append(meeting)
        elif object_type == "deals":
            for deal_truth in (*self._deals.values(), *self._orphans, *self._bulk_deals):
                deal = self.deal_record(deal_truth)
                if deal is not None:
                    records.append(deal)
        else:
            raise ValueError(f"unknown CRM object type {object_type!r}")
        matched: list[tuple[HubSpotRecord, Mapping[str, PropertyValue]]] = []
        for record in records:
            values = self.properties_of(record)
            if all(_filter_holds(_comparable(values.get(flt.property)), flt) for flt in filters):
                matched.append((record, values))
        matched.sort(key=lambda item: int(item[0].id))
        if sort_property is not None:
            present = [item for item in matched if item[1].get(sort_property) is not None]
            missing = [item for item in matched if item[1].get(sort_property) is None]
            present.sort(
                key=lambda item: cast(Any, _comparable(item[1].get(sort_property))),
                reverse=descending,
            )
            matched = present + missing
        return tuple(record for record, _values in matched)


# ---------------------------------------------------------------------------
# SimulatedWorld
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """The default clock: the current instant in UTC."""
    return datetime.now(UTC)


class SimulatedWorld:
    """A seeded, deterministic world behind every simulated source (AAP 0.3.3, FR-11).

    Two worlds with the same seed, spec, content assets, mapping and clock return identical
    records for every read; another seed returns different numbers. Reads take the current
    instant from the injected ``clock`` only. Entity reads accept an instant ``at`` (default:
    now) and apply every :class:`PlatformChange` up to it. Daily reads take inclusive native
    date ranges (Google: the customer time zone; LinkedIn: UTC) and return only days that
    exist: within the history, and not after the current native date, which is partial.

    The only mutable state is caches and the per-fault counters of :meth:`consume_fault`, all
    lock-guarded, so concurrent requests on one transport are safe.

    Args:
        seed: The world seed (``MOCK_SEED``, default 42).
        scenario: A :class:`WorldSpec`, or anything with ``name`` and ``spec`` (such as
            ``scenarios.Scenario``).
        clock: Returns the current tz-aware instant; ``datetime.now(UTC)`` when ``None``.
        content_assets: The white paper registry; loaded once with :meth:`Thresholds.load`
            when ``None``.
        mapping: The HubSpot MQL/QL mapping; loaded with :meth:`Thresholds.load` when ``None``.
        google_crm_properties: ``(campaign property, touch-date property)``; overrides the
            spec's Google CRM link when given.

    Raises:
        ValueError: When an explicitly passed content asset lies outside the reserved LinkedIn
            ranges, an asset form or override creative is missing from the spec, a platform
            change names an unknown entity, or a removed stage label is unknown.
        ConfigError: When the config files are needed and invalid.
    """

    def __init__(
        self,
        seed: int,
        scenario: WorldSpec | ScenarioLike = DEFAULT_SPEC,
        *,
        clock: Callable[[], datetime] | None = None,
        content_assets: Sequence[ContentAsset] | None = None,
        mapping: HubSpotMapping | None = None,
        google_crm_properties: tuple[str, str] | None = None,
    ) -> None:
        _check_int("seed", seed, minimum=None)
        spec = scenario if isinstance(scenario, WorldSpec) else scenario.spec
        _require(isinstance(spec, WorldSpec), "scenario must be a WorldSpec or carry one as .spec")
        self._seed = seed
        self._spec = spec
        self._core = _Core(seed, spec, clock if clock is not None else _utc_now)
        loaded: Thresholds | None = None
        if content_assets is None or mapping is None:
            loaded = Thresholds.load()
        self._mapping = mapping if mapping is not None else cast(Thresholds, loaded).mapping
        self._google = None if spec.google is None else _GoogleModel(self._core, spec.google)
        self._linkedin = (
            None if spec.linkedin is None else _LinkedInModel(self._core, spec.linkedin)
        )
        if content_assets is not None:
            self._assets = self._validate_assets(tuple(content_assets), explicit=True)
        else:
            self._assets = self._validate_assets(
                cast(Thresholds, loaded).content_assets, explicit=False
            )
        self._validate_changes()
        if google_crm_properties is None and spec.hubspot is not None:
            google_crm_properties = spec.hubspot.google_crm_properties
        self._google_properties = google_crm_properties
        self._crm = (
            None
            if spec.hubspot is None
            else _CrmModel(
                self._core,
                spec.hubspot,
                self._mapping,
                self._linkedin,
                self._google,
                google_crm_properties,
            )
        )
        self._fault_lock = threading.Lock()
        self._fault_remaining: list[int | None] = [fault.times for fault in spec.faults]
        _log.info(
            "simulation.world_built",
            name=spec.name,
            seed=seed,
            anchor=self._core.anchor.isoformat(),
            google_campaigns=0 if spec.google is None else len(spec.google.campaigns),
            google_bulk_campaigns=0
            if spec.google is None or spec.google.bulk is None
            else spec.google.bulk.campaigns,
            google_keywords=0 if spec.google is None else len(spec.google.keywords),
            google_search_terms=0 if spec.google is None else len(spec.google.search_terms),
            linkedin_accounts=0 if spec.linkedin is None else len(spec.linkedin.accounts),
            linkedin_campaigns=0 if spec.linkedin is None else len(spec.linkedin.campaigns),
            linkedin_creatives=0 if spec.linkedin is None else len(spec.linkedin.creatives),
            linkedin_lead_forms=0 if spec.linkedin is None else len(spec.linkedin.lead_forms),
            hubspot_stages=0 if self._crm is None else len(self._crm.options),
            content_assets=len(self._assets),
            faults=len(spec.faults),
            platform_changes=len(spec.platform_changes),
        )
        if self._crm is not None and self._crm.added_labels:
            _log.info(
                "simulation.stages_added",
                name=spec.name,
                labels=list(self._crm.added_labels),
            )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        scenario: WorldSpec | ScenarioLike = DEFAULT_SPEC,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> SimulatedWorld:
        """Build the world the running product simulates from its settings.

        ``MOCK_SEED`` seeds it; the Google CRM link follows
        ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY``/``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` when both
        are set, so a configured link is simulated with the portal's own property names.
        """
        properties: tuple[str, str] | None = None
        campaign_property = settings.hubspot_google_campaign_property
        touch_property = settings.hubspot_google_touch_date_property
        if campaign_property and touch_property:
            properties = (campaign_property, touch_property)
        return cls(settings.mock_seed, scenario, clock=clock, google_crm_properties=properties)

    # -- validation ------------------------------------------------------------------------------

    def _validate_assets(
        self, assets: tuple[ContentAsset, ...], *, explicit: bool
    ) -> tuple[ContentAsset, ...]:
        """Check the registry against the reserved ranges and the spec.

        The simulated world reuses exactly the URNs of ``config/content_assets.yaml``, so
        every registry form must be a spec form and every override creative a spec creative.
        """
        outside: list[str] = []
        for asset in assets:
            for urn in asset.lead_form_urns:
                if not LINKEDIN_FORM_IDS.contains(urn.removeprefix(FORM_URN_PREFIX)):
                    outside.append(f"{asset.slug}: {urn} lies outside LINKEDIN_FORM_IDS")
            for urn in asset.creative_urns:
                if not LINKEDIN_CREATIVE_IDS.contains(urn.removeprefix(CREATIVE_URN_PREFIX)):
                    outside.append(f"{asset.slug}: {urn} lies outside LINKEDIN_CREATIVE_IDS")
        if outside:
            if explicit:
                raise ValueError(
                    "content assets must use the reserved simulated LinkedIn ids: "
                    + "; ".join(outside)
                )
            # A config/content_assets.yaml edited for a live LinkedIn account names real
            # forms. Simulating another source must still work (nothing gates the build), so
            # the registry is set aside for the simulated world instead of failing it; the
            # white papers of a simulated LinkedIn dataset then resolve to no asset.
            _log.warning(
                "simulation.assets_not_simulated",
                reason="content_assets.yaml names LinkedIn ids outside the simulated ranges",
                assets=len(assets),
            )
            return ()
        if self._linkedin is not None:
            missing: list[str] = []
            creative_ids = set(self._linkedin.creative_ids.values())
            for asset in assets:
                for urn in asset.lead_form_urns:
                    if int(urn.removeprefix(FORM_URN_PREFIX)) not in self._linkedin.forms:
                        missing.append(f"{asset.slug}: lead form {urn}")
                for urn in asset.creative_urns:
                    if int(urn.removeprefix(CREATIVE_URN_PREFIX)) not in creative_ids:
                        missing.append(f"{asset.slug}: creative {urn}")
            if missing:
                raise ValueError(
                    f"world {self._spec.name!r} lacks the registry's LinkedIn entities: "
                    + "; ".join(missing)
                    + " (pass content_assets explicitly for a world without them)"
                )
        return assets

    def _validate_changes(self) -> None:
        """Check that every platform change names an entity (and budget) of the spec.

        Raises:
            ValueError: Naming the first unknown entity.
        """
        google = self._google
        linkedin = self._linkedin
        for change in self._spec.platform_changes:
            known: Mapping[str, object] | None = None
            if change.source == "google" and google is not None:
                google_entities: dict[str, Mapping[str, object]] = {
                    "campaign": google.campaigns,
                    "budget": google.budgets,
                    "ad_group": google.ad_groups,
                    "keyword": google.keywords,
                }
                known = google_entities.get(change.entity)
                if change.field == "budget_key":
                    _require(
                        str(change.value) in google.budgets,
                        f"platform change moves {change.key} to unknown budget {change.value!r}",
                    )
                if change.field == "amount" and change.key in google.budgets:
                    _to_micros(cast(Decimal, change.value))
            elif change.source == "linkedin" and linkedin is not None:
                linkedin_entities: dict[str, Mapping[str, object]] = {
                    "campaign": linkedin.campaigns,
                    "campaign_group": linkedin.groups,
                    "creative": linkedin.creatives,
                }
                known = linkedin_entities.get(change.entity)
            _require(
                known is not None and change.key in known,
                f"platform change names unknown {change.source} {change.entity} {change.key!r}",
            )

    # -- identity and time ------------------------------------------------------------------------

    @property
    def seed(self) -> int:
        """The world seed."""
        return self._seed

    @property
    def spec(self) -> WorldSpec:
        """The spec the world was built from."""
        return self._spec

    @property
    def name(self) -> str:
        """The spec name."""
        return self._spec.name

    @property
    def anchor(self) -> date:
        """The date that spec day offsets are relative to."""
        return self._core.anchor

    @property
    def history_start(self) -> date:
        """The first day with data (``anchor - history_days``)."""
        return self._core.history_start

    @property
    def content_assets(self) -> tuple[ContentAsset, ...]:
        """The white paper registry the world was validated against."""
        return self._assets

    @property
    def mapping(self) -> HubSpotMapping:
        """The HubSpot MQL/QL mapping the portal's stages follow."""
        return self._mapping

    @property
    def google_crm_properties(self) -> tuple[str, str] | None:
        """The simulated Google CRM link properties, or ``None``."""
        return self._google_properties

    def now(self) -> datetime:
        """The current instant (UTC) from the injected clock."""
        return self._core.now()

    @property
    def google_customer_id(self) -> str:
        """The simulated Google customer id (10 digits).

        Raises:
            LookupError: When the world has no Google part.
        """
        return self._require_google().spec.customer_id

    @property
    def linkedin_account_ids(self) -> tuple[str, ...]:
        """The simulated LinkedIn account ids, as the LinkedIn client takes them."""
        if self._linkedin is None:
            return ()
        return tuple(str(account_id) for account_id in self._linkedin.accounts)

    def currency_for(self, source: Source, account: int | str | None = None) -> str | None:
        """The currency of a source's account.

        Google's customer currency, a LinkedIn account's (the first account's when
        ``account`` is ``None``) or HubSpot's deal currency; ``None`` when the world does not
        simulate it.
        """
        if source == "google":
            return None if self._google is None else self._google.spec.currency
        if source == "linkedin":
            if self._linkedin is None:
                return None
            if account is None:
                accounts = list(self._linkedin.accounts.values())
                return accounts[0].currency if accounts else None
            try:
                return self._linkedin.account_of(account).currency
            except KeyError:
                return None
        return None if self._crm is None else self._crm.spec.deal_currency

    def state_at(self, at: datetime) -> WorldState:
        """The platform state at an instant: spec values with every change up to it applied."""
        return self._core.state_at(at)

    def _at(self, at: datetime | None) -> datetime:
        return self.now() if at is None else _require_aware("at", at)

    # -- faults ---------------------------------------------------------------------------------

    def faults_for(self, source: Source, surface: str) -> tuple[Fault, ...]:
        """The spec faults of one surface that can still apply, in spec order."""
        with self._fault_lock:
            return tuple(
                fault
                for fault, remaining in zip(self._spec.faults, self._fault_remaining, strict=True)
                if fault.source == source
                and fault.surface == surface
                and (remaining is None or remaining > 0)
            )

    def consume_fault(self, fault: Fault) -> bool:
        """Claim one application of ``fault``; ``False`` once its ``times`` are used up.

        A fault without ``times`` always applies. Counters are per world and lock-guarded.

        Raises:
            ValueError: When ``fault`` is not one of the spec's faults.
        """
        with self._fault_lock:
            candidates = [
                index for index, known in enumerate(self._spec.faults) if known is fault
            ] or [index for index, known in enumerate(self._spec.faults) if known == fault]
            _require(bool(candidates), "the fault is not part of this world's spec")
            for index in candidates:
                remaining = self._fault_remaining[index]
                if remaining is None:
                    return True
                if remaining > 0:
                    self._fault_remaining[index] = remaining - 1
                    return True
            return False

    # -- Google -----------------------------------------------------------------------------------

    def _require_google(self) -> _GoogleModel:
        if self._google is None:
            raise LookupError(f"world {self.name!r} has no Google part")
        return self._google

    def google_customer(self) -> GoogleCustomerRecord:
        """The customer (Q-G1).

        Raises:
            LookupError: When the world has no Google part.
        """
        spec = self._require_google().spec
        return GoogleCustomerRecord(
            customer_id=spec.customer_id,
            descriptive_name=spec.name,
            currency_code=spec.currency,
            time_zone=spec.time_zone,
        )

    def google_campaigns(self, at: datetime | None = None) -> tuple[GoogleCampaignRecord, ...]:
        """Every campaign listed at ``at`` (every status), explicit then bulk, ids ascending.

        Q-G2 filters out ``REMOVED``; Q-G2b reads by id with no status filter.
        """
        return self._require_google().campaign_records(self._at(at))

    def google_budgets(self, at: datetime | None = None) -> tuple[GoogleBudgetRecord, ...]:
        """Every explicit budget at ``at`` with its member campaigns."""
        return self._require_google().budget_records(self._at(at))

    def google_ad_groups(self, at: datetime | None = None) -> tuple[GoogleAdGroupRecord, ...]:
        """Every ad group listed at ``at`` (Q-G4 filters out ``REMOVED``)."""
        return self._require_google().ad_group_records(self._at(at))

    def google_keywords(self, at: datetime | None = None) -> tuple[GoogleKeywordRecord, ...]:
        """Every keyword listed at ``at``, keywords without traffic included (Q-G7)."""
        return self._require_google().keyword_records(self._at(at))

    def google_conversion_actions(
        self, at: datetime | None = None
    ) -> tuple[GoogleConversionActionRecord, ...]:
        """Every conversion action at ``at``, removed ones included (Q-G8)."""
        return self._require_google().conversion_action_records(self._at(at))

    def google_campaign_daily(
        self, start: date, end: date, *, campaign_ids: tuple[int, int] | None = None
    ) -> tuple[GoogleCampaignDay, ...]:
        """Q-G3 rows over ``[start, end]`` (account time zone); removed campaigns included.

        Args:
            start: First native date.
            end: Last native date (inclusive).
            campaign_ids: Optional inclusive ``campaign.id`` range, so id-range partitions of
                bulk worlds never build rows they drop.
        """
        return self._require_google().campaign_days(start, end, campaign_ids)

    def google_keyword_daily(
        self,
        start: date,
        end: date,
        *,
        campaign_ids: tuple[int, int] | None = None,
        ad_group_ids: tuple[int, int] | None = None,
    ) -> tuple[GoogleKeywordDay, ...]:
        """Q-G5 rows over ``[start, end]``, optionally within id ranges."""
        return self._require_google().keyword_days(start, end, campaign_ids, ad_group_ids)

    def google_search_term_daily(
        self,
        start: date,
        end: date,
        *,
        campaign_ids: tuple[int, int] | None = None,
        ad_group_ids: tuple[int, int] | None = None,
    ) -> tuple[GoogleSearchTermDay, ...]:
        """Q-G6 rows over ``[start, end]``, optionally within id ranges."""
        return self._require_google().search_term_days(start, end, campaign_ids, ad_group_ids)

    # -- LinkedIn ---------------------------------------------------------------------------------

    def _require_linkedin(self) -> _LinkedInModel:
        if self._linkedin is None:
            raise LookupError(f"world {self.name!r} has no LinkedIn part")
        return self._linkedin

    def _account(self, account_id: int | str) -> int:
        return self._require_linkedin().account_of(account_id).id

    def linkedin_accounts(self) -> tuple[LinkedInAccountRecord, ...]:
        """Every simulated ad account, in id order."""
        model = self._require_linkedin()
        return tuple(
            LinkedInAccountRecord(
                id=account.id,
                name=account.name,
                currency=account.currency,
                status="ACTIVE",
                type="BUSINESS",
                created_at=model.created_at(index),
            )
            for index, account in enumerate(sorted(model.accounts.values(), key=lambda a: a.id))
        )

    def linkedin_campaign_groups(
        self, account_id: int | str, at: datetime | None = None
    ) -> tuple[LinkedInCampaignGroupRecord, ...]:
        """Every campaign group of an account at ``at`` (every status)."""
        return self._require_linkedin().group_records(self._account(account_id), self._at(at))

    def linkedin_campaigns(
        self, account_id: int | str, at: datetime | None = None
    ) -> tuple[LinkedInCampaignRecord, ...]:
        """Every campaign of an account at ``at`` (every status, ``COMPLETED`` included)."""
        return self._require_linkedin().campaign_records(self._account(account_id), self._at(at))

    def linkedin_creatives(
        self,
        account_id: int | str,
        campaign_ids: Iterable[int | str] | None = None,
        at: datetime | None = None,
    ) -> tuple[LinkedInCreativeRecord, ...]:
        """Creatives of an account at ``at``, optionally only those of the given campaigns."""
        wanted = (
            None
            if campaign_ids is None
            else [
                _plain_id(value, "urn:li:sponsoredCampaign:", "campaign id")
                for value in campaign_ids
            ]
        )
        return self._require_linkedin().creative_records(
            self._account(account_id), wanted, self._at(at)
        )

    def linkedin_posts(self, urns: Iterable[str]) -> tuple[LinkedInPostRecord, ...]:
        """The posts referenced by creatives, for known ``urn:li:share:{id}`` URNs."""
        return self._require_linkedin().post_records(urns)

    def linkedin_creative_daily(
        self, account_id: int | str, start: date, end: date
    ) -> tuple[LinkedInCreativeDay, ...]:
        """Daily creative analytics of an account over ``[start, end]`` (UTC dates)."""
        return self._require_linkedin().creative_days(self._account(account_id), start, end)

    def linkedin_segments(
        self,
        account_id: int | str,
        pivot: str,
        start: date,
        end: date,
        *,
        campaign_ids: Iterable[int | str] | None = None,
    ) -> tuple[LinkedInSegmentRecord, ...]:
        """Demographic aggregates per (campaign, value) for exactly ``[start, end]``.

        Approximated and privacy-thresholded (top values only, none below the minimum), with
        lead metrics ``None`` for a pivot the spec marks as not returning them.
        """
        wanted = (
            None
            if campaign_ids is None
            else [
                _plain_id(value, "urn:li:sponsoredCampaign:", "campaign id")
                for value in campaign_ids
            ]
        )
        return self._require_linkedin().segment_records(
            self._account(account_id), pivot, start, end, wanted
        )

    def linkedin_lead_forms(
        self, account_id: int | str, at: datetime | None = None
    ) -> tuple[LinkedInLeadFormRecord, ...]:
        """Lead forms owned by an account, each at its version current at ``at``."""
        return self._require_linkedin().form_records(self._account(account_id), self._at(at))

    def linkedin_form_version(
        self, form_id: int | str, version: int
    ) -> LinkedInFormVersionRecord | None:
        """One version of a lead form, or ``None`` when the form or version is unknown."""
        model = self._require_linkedin()
        identifier = _plain_id(form_id, FORM_URN_PREFIX, "form id")
        form = model.forms.get(identifier)
        if form is None:
            return None
        for candidate in form.versions:
            if candidate.version == version:
                return model.version_record(identifier, candidate)
        return None

    def linkedin_lead_responses(
        self, account_id: int | str, start_ms: int, end_ms: int
    ) -> tuple[LinkedInLeadResponse, ...]:
        """Responses of an account submitted in ``[start_ms, end_ms)`` (epoch ms) and by now.

        Test leads are included, flagged ``test_lead``; responses are ordered by submission.
        """
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return self._require_linkedin().responses(
            self._account(account_id),
            epoch + timedelta(milliseconds=start_ms),
            epoch + timedelta(milliseconds=end_ms),
        )

    def linkedin_lead_response(self, response_id: str) -> LinkedInLeadResponse | None:
        """One response by plain id, or ``None`` when unknown or not yet submitted."""
        return self._require_linkedin().response(response_id)

    # -- HubSpot ----------------------------------------------------------------------------------

    def _require_crm(self) -> _CrmModel:
        if self._crm is None:
            raise LookupError(f"world {self.name!r} has no HubSpot part")
        return self._crm

    def hubspot_lifecycle_options(self) -> tuple[PropertyOption, ...]:
        """The portal's ``lifecyclestage`` options, in display order."""
        return self._require_crm().options

    def hubspot_property(self, object_type: str, name: str) -> PropertyRecord | None:
        """A property definition of ``contacts``, ``meetings`` or ``deals``, or ``None``."""
        return self._require_crm().properties.get((object_type, name))

    def hubspot_properties(self, record: HubSpotRecord) -> Mapping[str, PropertyValue]:
        """Every property of a contact, meeting or deal record by HubSpot name, typed."""
        return self._require_crm().properties_of(record)

    def hubspot_contact_by_email_hash(self, sha256_hex: str) -> ContactRecord | None:
        """The simulated person's contact for a normalised-email SHA-256, once it exists."""
        crm = self._require_crm()
        truth = crm.contact_for_hash(sha256_hex)
        return None if truth is None else crm.contact_record(truth)

    def hashed_contact_for(self, sha256_hex: str) -> ContactRecord | None:
        """A seeded contact answering a real lead email's hash, or ``None``.

        Used when HubSpot is simulated and LinkedIn live: a deterministic
        ``live_lead_match_share`` of hashes get a contact.

        The world never sees the plaintext: the record's ``email`` is ``None`` and its
        ``email_sha256`` is the hash asked for.

        Raises:
            ValueError: When the value is not a SHA-256 hex digest.
        """
        crm = self._require_crm()
        offset = crm.hashed_offset(sha256_hex)
        if offset is None:
            return None
        truth = crm.contact_truth(str(HUBSPOT_HASHED_CONTACT_IDS.at(offset)))
        if truth is None:
            return None
        return crm.contact_record(truth, email_sha256=sha256_hex.strip().lower())

    def hubspot_contacts_by_id(self, ids: Iterable[str]) -> tuple[ContactRecord, ...]:
        """Existing contacts for ``ids``, in request order, unknown ids skipped."""
        crm = self._require_crm()
        records: list[ContactRecord] = []
        for contact_id in ids:
            truth = crm.contact_truth(str(contact_id))
            record = None if truth is None else crm.contact_record(truth)
            if record is not None:
                records.append(record)
        return tuple(records)

    def hubspot_meetings(self, ids: Iterable[str]) -> tuple[MeetingRecord, ...]:
        """Existing meetings for ``ids``, in request order, unknown ids skipped."""
        crm = self._require_crm()
        records: list[MeetingRecord] = []
        for meeting_id in ids:
            truth = crm.meeting_truth(str(meeting_id))
            record = None if truth is None else crm.meeting_record(truth)
            if record is not None:
                records.append(record)
        return tuple(records)

    def hubspot_deals(self, ids: Iterable[str]) -> tuple[DealRecord, ...]:
        """Existing deals for ``ids``, in request order, unknown ids skipped."""
        crm = self._require_crm()
        records: list[DealRecord] = []
        for deal_id in ids:
            truth = crm.deal_truth(str(deal_id))
            record = None if truth is None else crm.deal_record(truth)
            if record is not None:
                records.append(record)
        return tuple(records)

    def hubspot_associations(self, from_type: str, to_type: str, object_id: str) -> tuple[str, ...]:
        """Associated object ids that exist now, ascending.

        Supported: contacts to meetings and deals, meetings and deals to contacts. An empty
        tuple is what HubSpot reports as a 207 ``NO_ASSOCIATIONS_FOUND`` error for that input.

        Raises:
            ValueError: For an unsupported pair of object types.
        """
        return self._require_crm().associations(from_type, to_type, str(object_id))

    def hubspot_search(
        self,
        object_type: str,
        filters: Sequence[SearchFilter],
        *,
        sort_property: str | None = None,
        descending: bool = False,
    ) -> tuple[HubSpotRecord, ...]:
        """Every existing record of ``contacts``, ``meetings`` or ``deals`` matching filters.

        ``filters`` form one AND group. Results are sorted by ``sort_property`` (records
        without a value last, then by id) or by id.

        Seeded contacts answering real lead emails are found by email lookups only, never by
        search. ``total`` is the length of the result; paging is the caller's.
        """
        return self._require_crm().search(object_type, filters, sort_property, descending)

    # -- invariants --------------------------------------------------------------------------------

    def self_check(self, days: int = 35) -> tuple[str, ...]:
        """Check the world's invariants over its most recent ``days`` days.

        * every simulated id lies in its reserved range;
        * LinkedIn daily ``leads`` equal the non-test lead responses per creative and UTC day,
          form opens are at least leads, and landing-page creatives have no responses;
        * Google search-term spend is at most keyword spend per (ad group, day), keyword
          spend at most campaign spend per (campaign, day), conversions carry at most six
          decimals, and no day spends more than twice its daily budget;
        * every simulated email (lead responses, contacts) ends with ``@sim.invalid``.

        Bulk filler has fixed metrics that satisfy the spend invariants by construction and is
        left out of the per-day checks.

        Returns:
            A description of every violation; empty when the world is consistent.
        """
        _check_int("days", days, minimum=1)
        problems: list[str] = []
        identifiers: list[tuple[str, int | str, IdRange]] = []
        if self._google is not None:
            identifiers += self._google.all_ids()
        if self._linkedin is not None:
            identifiers += self._linkedin.all_ids()
        if self._crm is not None:
            identifiers += [
                ("lifecycle stage", option.value, HUBSPOT_STAGE_IDS)
                for option in self._crm.options
                if option.value.isdecimal()
            ]
        for kind, value, reserved in identifiers:
            if not reserved.contains(value):
                problems.append(f"{kind} id {value} lies outside its reserved range")
        if self._linkedin is not None:
            problems += self._check_linkedin(days)
        if self._google is not None:
            problems += self._check_google(days)
        if self._crm is not None:
            problems += self._check_crm()
        return tuple(problems)

    def _check_linkedin(self, days: int) -> list[str]:
        model = cast(_LinkedInModel, self._linkedin)
        problems: list[str] = []
        end = self._core.today(None)
        start = end - timedelta(days=days - 1)
        now = self.now()
        for account_id in model.accounts:
            for row in model.creative_days(account_id, start, end):
                key = model.creative_by_id[row.creative_id]
                responses = model.responses_by_creative_day.get((key, row.date), [])
                real = sum(
                    1
                    for response in responses
                    if not response.test_lead and response.submitted_at <= now
                )
                if real != row.leads:
                    problems.append(
                        f"LinkedIn creative {row.creative_id} on {row.date}: {row.leads} leads "
                        f"but {real} non-test responses"
                    )
                if row.form_opens < row.leads:
                    problems.append(
                        f"LinkedIn creative {row.creative_id} on {row.date}: opens < leads"
                    )
                if row.clicks > row.impressions:
                    problems.append(
                        f"LinkedIn creative {row.creative_id} on {row.date}: clicks > impressions"
                    )
        for (key, day), responses in model.responses_by_creative_day.items():
            if model.creatives[key].lead_form_id is None and responses:
                problems.append(f"landing-page creative {key} has lead responses on {day}")
        for response in model.responses_by_id.values():
            if not is_simulated_email(response.email):
                problems.append(f"lead response {response.id} has a non-simulated email")
        return problems

    def _check_google(self, days: int) -> list[str]:
        model = cast(_GoogleModel, self._google)
        problems: list[str] = []
        end = self._core.today(model.tz)
        start = end - timedelta(days=days - 1)
        explicit = (
            (GOOGLE_CAMPAIGN_IDS.start, GOOGLE_CAMPAIGN_IDS.at(len(model.campaigns) - 1))
            if model.campaigns
            else None
        )
        if explicit is None:
            return problems
        campaign_rows = model.campaign_days(start, end, explicit)
        keyword_rows = model.keyword_days(start, end, explicit, None)
        term_rows = model.search_term_days(start, end, explicit, None)
        keyword_by_group: dict[tuple[date, int], Decimal] = {}
        keyword_by_campaign: dict[tuple[date, int], Decimal] = {}
        for keyword in keyword_rows:
            group_key = (keyword.date, keyword.ad_group_id)
            keyword_by_group[group_key] = keyword_by_group.get(group_key, Decimal(0)) + keyword.cost
            campaign_key = (keyword.date, keyword.campaign_id)
            keyword_by_campaign[campaign_key] = (
                keyword_by_campaign.get(campaign_key, Decimal(0)) + keyword.cost
            )
        term_by_group: dict[tuple[date, int], Decimal] = {}
        for term in term_rows:
            if term.criterion_id is None:
                continue
            group_key = (term.date, term.ad_group_id)
            term_by_group[group_key] = term_by_group.get(group_key, Decimal(0)) + term.cost
            if _decimal_places(term.conversions) > 6:
                problems.append(f"search term {term.search_term!r} has over six decimals")
        for group_key, cost in term_by_group.items():
            if cost > keyword_by_group.get(group_key, Decimal(0)):
                problems.append(
                    f"Google ad group {group_key[1]} on {group_key[0]}: search-term spend {cost} "
                    f"exceeds keyword spend {keyword_by_group.get(group_key, Decimal(0))}"
                )
        campaign_cost = {(row.date, row.campaign_id): row.cost for row in campaign_rows}
        for campaign_key, cost in keyword_by_campaign.items():
            if cost > campaign_cost.get(campaign_key, Decimal(0)):
                problems.append(
                    f"Google campaign {campaign_key[1]} on {campaign_key[0]}: keyword spend "
                    f"{cost} exceeds campaign spend"
                )
        keys_by_id = {value: key for key, value in model.campaign_ids.items()}
        for row in campaign_rows:
            if _decimal_places(row.conversions) > 6:
                problems.append(f"Google campaign {row.campaign_id} has over six decimals")
            if row.clicks > row.impressions:
                problems.append(
                    f"Google campaign {row.campaign_id} on {row.date}: clicks > impressions"
                )
            daily = model.daily_budget_on(keys_by_id[row.campaign_id], row.date)
            if daily is not None and row.cost > 2 * daily:
                problems.append(
                    f"Google campaign {row.campaign_id} on {row.date}: spend {row.cost} exceeds "
                    f"twice its daily budget {daily}"
                )
        return problems

    def _check_crm(self) -> list[str]:
        crm = cast(_CrmModel, self._crm)
        crm.search("contacts", (), None, False)
        problems: list[str] = []
        for contact in crm._contacts.values():
            if not HUBSPOT_CONTACT_IDS.contains(contact.id):
                problems.append(f"contact {contact.id} lies outside HUBSPOT_CONTACT_IDS")
            if contact.email is not None and not is_simulated_email(contact.email):
                problems.append(f"contact {contact.id} has a non-simulated email")
        for meeting_id in crm._meetings:
            if not HUBSPOT_MEETING_IDS.contains(meeting_id):
                problems.append(f"meeting {meeting_id} lies outside HUBSPOT_MEETING_IDS")
        for deal_id in (*crm._deals, *(deal.id for deal in crm._orphans)):
            if not HUBSPOT_DEAL_IDS.contains(deal_id):
                problems.append(f"deal {deal_id} lies outside HUBSPOT_DEAL_IDS")
        return problems
