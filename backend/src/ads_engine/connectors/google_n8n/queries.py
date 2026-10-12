"""GAQL templates for the Google Ads queries Q-G1 to Q-G8 and Q-G2b.

Every Google Ads read the product makes is one of the templates in :data:`QUERIES`. The existing
n8n Cloud webhook relays each one to ``googleAds:search`` v25, one GAQL query per webhook call.
The templates are read-only ``SELECT`` statements: the product never writes to Google Ads.

Completeness without a page token:
    ``googleAds:search`` returns at most :data:`PAGE_SIZE` (10,000) rows per page and the n8n
    response carries no page token, so a page of exactly 10,000 rows may be truncated, and a
    truncated page cannot reveal the ids it omitted. The templates therefore offer only
    narrowing forms whose completeness can be proven (``partition.py`` drives them):

    * keyset scans for Q-G2 and Q-G4,
      ``... AND {id} > {last_id} ORDER BY {id} LIMIT 9999``. Every page stays below the page
      size, so a keyset page can never be silently truncated;
    * an ``IN`` list of at most :data:`MAX_IDS_PER_IN` ids for Q-G2b;
    * ``segments.date BETWEEN`` ranges and numeric ``campaign.id`` and ``ad_group.id`` ranges
      for Q-G3, Q-G5 and Q-G6. They are ranges, never lists of known ids, so a campaign
      removed during the window is still covered;
    * a criterion keyset inside one ad group for Q-G5 and Q-G7, because
      ``ad_group_criterion.criterion_id`` is unique only within its ad group.

Safety of the text:
    Scope values are validated ``int`` ids in ``0 .. INT64_MAX`` and ``datetime.date`` values
    only, so no caller-supplied text is ever interpolated into GAQL. Enum and date literals are
    single-quoted. :meth:`QueryTemplate.render` is deterministic (one line, single spaces, a
    fixed condition order), so the simulator and the tests compare rendered strings.

Inverse parsing:
    :func:`parse_query` is the exact inverse of :meth:`QueryTemplate.render`. The simulator
    (``simulation/payloads.py``) uses it to honour date ranges, id ranges, ``last_id``, ``IN``
    lists and ``LIMIT`` when it renders rows, and the CLI's fixture capture uses
    :func:`query_id_for`, so the GAQL grammar lives in this one module.

Example::

    query = QUERIES["Q-G4"].render(last_id=0)
    # One line: SELECT ad_group.id, ad_group.name, ad_group.status, campaign.id FROM ad_group
    # WHERE ad_group.status != 'REMOVED' AND ad_group.id > 0 ORDER BY ad_group.id LIMIT 9999
    parsed = parse_query(query)  # ParsedQuery(query_id="Q-G4", scope={"last_id": 0})

Only the standard library is imported: ``parser``, ``partition``, the simulator and the CLI all
import this module, so it stays free of ``httpx``, settings and every other product module.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from types import MappingProxyType
from typing import Final, Literal

__all__ = [
    "AD_GROUP_HI",
    "AD_GROUP_ID",
    "AD_GROUP_LO",
    "CAMPAIGN_HI",
    "CAMPAIGN_LO",
    "END",
    "IDS",
    "INT64_MAX",
    "INT64_MIN_ID",
    "KEYSET_LIMIT",
    "LAST_ID",
    "MAX_IDS_PER_IN",
    "PAGE_SIZE",
    "QUERIES",
    "SCOPE_KEYS",
    "START",
    "ParsedQuery",
    "QueryTemplate",
    "Strategy",
    "parse_query",
    "query_id_for",
    "rest_path",
]

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

PAGE_SIZE: Final = 10_000
"""Fixed ``googleAds:search`` page size. A page of exactly this many rows with no page token
has unknown completeness and must be partitioned, never trusted as complete."""

KEYSET_LIMIT: Final = 9_999
"""``LIMIT`` of every keyset page. It sits below :data:`PAGE_SIZE`, so a keyset page can never
be silently truncated: a page of exactly 9,999 rows means "continue from its last id"."""

MAX_IDS_PER_IN: Final = 1_000
"""Most ids one Q-G2b ``IN`` list carries. A successful chunk is complete, because Q-G2b
returns at most one row per campaign."""

INT64_MAX: Final = 9_223_372_036_854_775_807
"""Largest INT64 value: the upper bound of every id and of the full id range."""

INT64_MIN_ID: Final = 0
"""Lower bound of every id and of the full id range. Google Ads ids are positive INT64 values,
so ranges start at 0 rather than at the INT64 minimum."""

Strategy = Literal["single", "keyset", "by_ids", "ad_group_ranges", "dated"]
"""How ``partition.py`` reads a template to completion.

* ``single``: one call (Q-G1, Q-G8).
* ``keyset``: ``id > last_id ORDER BY id LIMIT 9999`` pages (Q-G2, Q-G4).
* ``by_ids``: ``IN`` lists of at most :data:`MAX_IDS_PER_IN` ids (Q-G2b).
* ``ad_group_ranges``: ``ad_group.id`` range halving, then a criterion keyset inside one ad
  group (Q-G7).
* ``dated``: date halving, then ``campaign.id`` and ``ad_group.id`` range halving, and for
  Q-G5 a criterion keyset inside one (day, ad group) (Q-G3, Q-G5, Q-G6).
"""

# Scope keys accepted by ``QueryTemplate.render`` and returned by ``parse_query``.
LAST_ID: Final = "last_id"
"""Keyset position: rows with a keyset id strictly greater than this are returned."""

IDS: Final = "ids"
"""Q-G2b: the campaign ids of one ``IN`` list."""

START: Final = "start"
"""First native date of a dated query, inclusive."""

END: Final = "end"
"""Last native date of a dated query, inclusive."""

CAMPAIGN_LO: Final = "campaign_lo"
"""Lowest ``campaign.id`` of a numeric campaign range, inclusive."""

CAMPAIGN_HI: Final = "campaign_hi"
"""Highest ``campaign.id`` of a numeric campaign range, inclusive."""

AD_GROUP_LO: Final = "ad_group_lo"
"""Lowest ``ad_group.id`` of a numeric ad-group range, inclusive."""

AD_GROUP_HI: Final = "ad_group_hi"
"""Highest ``ad_group.id`` of a numeric ad-group range, inclusive."""

AD_GROUP_ID: Final = "ad_group_id"
"""The single ad group a criterion keyset scans (Q-G5, Q-G7); always paired with ``last_id``."""

SCOPE_KEYS: Final[frozenset[str]] = frozenset(
    {LAST_ID, IDS, START, END, CAMPAIGN_LO, CAMPAIGN_HI, AD_GROUP_LO, AD_GROUP_HI, AD_GROUP_ID}
)
"""Every scope key any template accepts. Each template accepts a subset of these."""

# ---------------------------------------------------------------------------------------------
# REST key paths
# ---------------------------------------------------------------------------------------------

_SEGMENT: Final = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_FIELD_RE: Final = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})*")


def rest_path(gaql_field: str) -> tuple[str, ...]:
    """Return the REST JSON key path of a GAQL field.

    A REST ``GoogleAdsRow`` nests one object per dot segment with lowerCamelCase keys, so each
    snake_case segment is converted on its own. ``parser.get_field`` walks rows with this path
    and the simulator builds rows with it.

    Example::

        rest_path("campaign_budget.amount_micros")  # ("campaignBudget", "amountMicros")
        rest_path("segments.keyword.info.match_type")
        # ("segments", "keyword", "info", "matchType")
        rest_path("ad_group_criterion.quality_info.quality_score")
        # ("adGroupCriterion", "qualityInfo", "qualityScore")

    Args:
        gaql_field: A GAQL field or resource name in snake_case, such as ``"campaign.id"``.

    Returns:
        One lowerCamelCase key per dot segment.

    Raises:
        ValueError: When ``gaql_field`` is not a dot-separated list of lowercase snake_case
            segments.
    """
    return _rest_path(gaql_field)


@lru_cache(maxsize=512)
def _rest_path(gaql_field: str) -> tuple[str, ...]:
    """Cached body of :func:`rest_path`; the parser calls it for every field of every row."""
    if _FIELD_RE.fullmatch(gaql_field) is None:
        raise ValueError("GAQL field names are dot-separated lowercase snake_case segments")
    return tuple(_lower_camel(segment) for segment in gaql_field.split("."))


def _lower_camel(segment: str) -> str:
    """Convert one snake_case segment to lowerCamelCase (``amount_micros`` -> ``amountMicros``)."""
    head, *rest = segment.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


# ---------------------------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------------------------


def _valid_id(query_id: str, key: str, value: object) -> int:
    """Validate one id-valued scope entry.

    Args:
        query_id: Template id, for the error message.
        key: Scope key, for the error message.
        value: Candidate id.

    Returns:
        The id as a plain ``int``.

    Raises:
        ValueError: When the value is not an ``int`` (``bool`` included) or lies outside
            ``0 .. INT64_MAX``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{query_id}: scope key {key!r} must be an int id, not {type(value).__name__}"
        )
    if not INT64_MIN_ID <= value <= INT64_MAX:
        raise ValueError(f"{query_id}: scope key {key!r} must lie between 0 and INT64_MAX")
    return int(value)


def _valid_date(query_id: str, key: str, value: object) -> date:
    """Validate one date-valued scope entry.

    Args:
        query_id: Template id, for the error message.
        key: Scope key, for the error message.
        value: Candidate date.

    Returns:
        The value as a plain ``datetime.date``.

    Raises:
        ValueError: When the value is not a ``datetime.date`` or is a ``datetime.datetime``. A
            datetime carries a time and a zone, and Google's ``segments.date`` is a native day.
    """
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(
            f"{query_id}: scope key {key!r} must be a datetime.date, not {type(value).__name__}"
        )
    return date(value.year, value.month, value.day)


def _valid_ids(query_id: str, key: str, value: object) -> tuple[int, ...]:
    """Validate the ``ids`` scope entry of Q-G2b.

    Args:
        query_id: Template id, for the error message.
        key: Scope key, for the error message.
        value: Candidate collection of ids.

    Returns:
        The distinct ids, sorted ascending.

    Raises:
        ValueError: When the value is not a collection of ids (strings and mappings are
            rejected), holds fewer than 1 or more than :data:`MAX_IDS_PER_IN` ids, holds an
            invalid id or repeats one.
    """
    if isinstance(value, str | bytes | bytearray | Mapping) or not isinstance(value, Collection):
        raise ValueError(f"{query_id}: scope key {key!r} must be a collection of int ids")
    if not 1 <= len(value) <= MAX_IDS_PER_IN:
        raise ValueError(
            f"{query_id}: scope key {key!r} must hold between 1 and {MAX_IDS_PER_IN} ids"
        )
    ids = [_valid_id(query_id, key, item) for item in value]
    distinct = sorted(set(ids))
    if len(distinct) != len(ids):
        raise ValueError(f"{query_id}: scope key {key!r} must not repeat an id")
    return tuple(distinct)


# ---------------------------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------------------------

_KeysetMode = Literal["none", "always", "per_ad_group"]


@dataclass(frozen=True, slots=True)
class _Form:
    """Declarative WHERE grammar of one template.

    ``QueryTemplate.render`` emits the conditions in exactly this attribute order, and
    ``parse_query`` reads them back in the same order.

    Attributes:
        fixed: Literal conditions that always come first, such as the ``REMOVED`` exclusion.
        dated: ``segments.date BETWEEN '{start}' AND '{end}'``; ``start`` and ``end`` required.
        campaign_range: Optional ``campaign.id >= {campaign_lo} AND campaign.id <= {campaign_hi}``.
        ad_group_range: Optional ``ad_group.id >= {ad_group_lo} AND ad_group.id <= {ad_group_hi}``.
        keyset: ``always`` renders ``{keyset_field} > {last_id}`` with ``last_id`` required.
            ``per_ad_group`` optionally renders ``ad_group.id = {ad_group_id} AND
            {keyset_field} > {last_id}``, with both keys together and never with the ad-group
            range. Both end the query in ``ORDER BY {keyset_field} LIMIT 9999``.
        id_list: Field of a required ``IN ({ids})`` list, or ``None``.
    """

    fixed: tuple[str, ...] = ()
    dated: bool = False
    campaign_range: bool = False
    ad_group_range: bool = False
    keyset: _KeysetMode = "none"
    id_list: str | None = None

    @property
    def allowed(self) -> frozenset[str]:
        """Scope keys this grammar accepts."""
        keys: set[str] = set()
        if self.dated:
            keys |= {START, END}
        if self.campaign_range:
            keys |= {CAMPAIGN_LO, CAMPAIGN_HI}
        if self.ad_group_range:
            keys |= {AD_GROUP_LO, AD_GROUP_HI}
        if self.keyset == "always":
            keys.add(LAST_ID)
        elif self.keyset == "per_ad_group":
            keys |= {AD_GROUP_ID, LAST_ID}
        if self.id_list is not None:
            keys.add(IDS)
        return frozenset(keys)

    @property
    def required(self) -> frozenset[str]:
        """Scope keys this grammar cannot render without."""
        keys: set[str] = set()
        if self.dated:
            keys |= {START, END}
        if self.keyset == "always":
            keys.add(LAST_ID)
        if self.id_list is not None:
            keys.add(IDS)
        return frozenset(keys)


@dataclass(frozen=True, slots=True)
class QueryTemplate:
    """One GAQL query of AAP 0.3.3 ("Queries issued") and the scopes it can be narrowed to.

    Attributes:
        query_id: Plan id, such as ``"Q-G2"``.
        resource: GAQL ``FROM`` resource, such as ``"campaign"``.
        select: Selected fields in snake_case GAQL names, in rendering order. It equals the
            ``request.gaql.select`` of the row contract named by ``row_surface``.
        row_surface: Name of the contract ``backend/tests/contracts/google/{row_surface}.json``
            that every result row follows, and the key of ``parser.PARSERS``.
        strategy: How ``partition.py`` reads the template to completion.
        keyset_field: Field a keyset form orders and filters by, or ``None`` without one.
    """

    query_id: str
    resource: str
    select: tuple[str, ...]
    row_surface: str
    strategy: Strategy
    keyset_field: str | None
    _form: _Form = field(default_factory=_Form, repr=False)

    def __post_init__(self) -> None:
        """Reject a template whose parts disagree, so a bad edit fails at import time."""
        if not self.select or len(set(self.select)) != len(self.select):
            raise ValueError(f"{self.query_id}: select must list distinct fields")
        for name in (self.resource, *self.select):
            rest_path(name)
        form = self._form
        if (self.keyset_field is None) != (form.keyset == "none"):
            raise ValueError(f"{self.query_id}: keyset_field must be set exactly for keyset forms")
        if (form.keyset == "always") != (self.strategy == "keyset"):
            raise ValueError(f"{self.query_id}: a keyset strategy needs a required keyset form")
        if (form.id_list is not None) != (self.strategy == "by_ids"):
            raise ValueError(f"{self.query_id}: a by_ids strategy needs an id list form")
        if form.dated != (self.strategy == "dated"):
            raise ValueError(f"{self.query_id}: a dated strategy needs a dated form")

    @property
    def allowed_scope_keys(self) -> frozenset[str]:
        """Scope keys :meth:`render` accepts for this template."""
        return self._form.allowed

    @property
    def required_scope_keys(self) -> frozenset[str]:
        """Scope keys :meth:`render` requires for this template."""
        return self._form.required

    @property
    def fixed_conditions(self) -> tuple[str, ...]:
        """Literal conditions every rendering carries, such as ``campaign.status != 'REMOVED'``.

        An empty tuple means the template has no status filter (Q-G1, Q-G2b, Q-G3, Q-G5, Q-G6,
        Q-G8), so ``REMOVED`` entities are returned. The simulator reads this instead of
        encoding its own status rules.
        """
        return self._form.fixed

    def render(self, **scope: object) -> str:
        """Render the canonical GAQL text of this template for one scope.

        The text is one line with single spaces: ``SELECT {fields} FROM {resource}``, then
        ``WHERE`` with the conditions joined by ``AND`` in a fixed order (literal conditions,
        date range, campaign range, ad-group range, keyset, ``IN`` list), then
        ``ORDER BY {keyset_field} LIMIT 9999`` for keyset forms. No trailing whitespace. Equal
        scopes always render equal text.

        Example::

            QUERIES["Q-G2b"].render(ids=[30, 10])
            # "SELECT campaign.id, ... FROM campaign WHERE campaign.id IN (10, 30)"
            QUERIES["Q-G3"].render(start=date(2026, 9, 1), end=date(2026, 9, 28))
            # "... WHERE segments.date BETWEEN '2026-09-01' AND '2026-09-28'"

        Args:
            **scope: Narrowing values, keyed by the scope-key constants of this module. Ids are
                ``int`` values in ``0 .. INT64_MAX``; ``start`` and ``end`` are
                ``datetime.date`` values; ``ids`` is a collection of distinct ids.

        Returns:
            The GAQL query text.

        Raises:
            ValueError: Naming the offending key when a key is unknown or not accepted by this
                template, a required key is missing, a range or keyset pair is incomplete,
                mutually exclusive keys are combined, or a value fails validation (``bool`` or
                out-of-range id, ``datetime`` instead of ``date``, ``start > end``, a range
                with ``lo > hi``, 0 or more than 1,000 ``ids``).
        """
        self._check_keys(scope)
        form = self._form
        conditions = list(form.fixed)
        if form.dated:
            start = _valid_date(self.query_id, START, scope[START])
            end = _valid_date(self.query_id, END, scope[END])
            if start > end:
                raise ValueError(f"{self.query_id}: scope key {START!r} must not follow {END!r}")
            conditions.append(
                f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
            )
        if form.campaign_range:
            campaigns = self._range(scope, CAMPAIGN_LO, CAMPAIGN_HI)
            if campaigns is not None:
                conditions.append(
                    f"campaign.id >= {campaigns[0]} AND campaign.id <= {campaigns[1]}"
                )
        if form.ad_group_range:
            ad_groups = self._range(scope, AD_GROUP_LO, AD_GROUP_HI)
            if ad_groups is not None:
                conditions.append(
                    f"ad_group.id >= {ad_groups[0]} AND ad_group.id <= {ad_groups[1]}"
                )
        order_by: str | None = None
        if form.keyset == "always" or LAST_ID in scope:
            order_by = self._keyset_column()
            if form.keyset == "per_ad_group":
                ad_group_id = _valid_id(self.query_id, AD_GROUP_ID, scope[AD_GROUP_ID])
                conditions.append(f"ad_group.id = {ad_group_id}")
            last_id = _valid_id(self.query_id, LAST_ID, scope[LAST_ID])
            conditions.append(f"{order_by} > {last_id}")
        if form.id_list is not None:
            ids = _valid_ids(self.query_id, IDS, scope[IDS])
            conditions.append(f"{form.id_list} IN ({', '.join(str(i) for i in ids)})")
        text = "SELECT " + ", ".join(self.select) + " FROM " + self.resource
        if conditions:
            text += " WHERE " + " AND ".join(conditions)
        if order_by is not None:
            text += f" ORDER BY {order_by} LIMIT {KEYSET_LIMIT}"
        return text

    def _check_keys(self, scope: Mapping[str, object]) -> None:
        """Reject unknown, unaccepted, missing, unpaired and mutually exclusive scope keys.

        Args:
            scope: The scope passed to :meth:`render`.

        Raises:
            ValueError: Naming the first offending key.
        """
        form = self._form
        for key in sorted(scope):
            if key not in SCOPE_KEYS:
                raise ValueError(f"{self.query_id}: unknown scope key {key!r}")
            if key not in form.allowed:
                raise ValueError(f"{self.query_id}: scope key {key!r} is not accepted here")
        missing = sorted(form.required - scope.keys())
        if missing:
            raise ValueError(f"{self.query_id}: scope key {missing[0]!r} is required")
        if form.keyset == "per_ad_group":
            has_ad_group, has_last = AD_GROUP_ID in scope, LAST_ID in scope
            if has_ad_group != has_last:
                absent, present = (LAST_ID, AD_GROUP_ID) if has_ad_group else (AD_GROUP_ID, LAST_ID)
                raise ValueError(
                    f"{self.query_id}: scope key {absent!r} is required with {present!r}"
                )
            if has_ad_group and (AD_GROUP_LO in scope or AD_GROUP_HI in scope):
                raise ValueError(
                    f"{self.query_id}: scope key {AD_GROUP_ID!r} is mutually exclusive with "
                    f"{AD_GROUP_LO!r} and {AD_GROUP_HI!r}"
                )

    def _range(
        self, scope: Mapping[str, object], lo_key: str, hi_key: str
    ) -> tuple[int, int] | None:
        """Validate an optional inclusive id range.

        Args:
            scope: The scope passed to :meth:`render`.
            lo_key: Key of the lower bound.
            hi_key: Key of the upper bound.

        Returns:
            ``(lo, hi)``, or ``None`` when neither bound is given.

        Raises:
            ValueError: When only one bound is given, a bound is not a valid id, or
                ``lo > hi``.
        """
        has_lo, has_hi = lo_key in scope, hi_key in scope
        if not has_lo and not has_hi:
            return None
        if has_lo != has_hi:
            absent, present = (hi_key, lo_key) if has_lo else (lo_key, hi_key)
            raise ValueError(f"{self.query_id}: scope key {absent!r} is required with {present!r}")
        lo = _valid_id(self.query_id, lo_key, scope[lo_key])
        hi = _valid_id(self.query_id, hi_key, scope[hi_key])
        if lo > hi:
            raise ValueError(f"{self.query_id}: scope key {lo_key!r} must not exceed {hi_key!r}")
        return lo, hi

    def _keyset_column(self) -> str:
        """Return the keyset field; ``__post_init__`` guarantees it for keyset forms."""
        if self.keyset_field is None:
            raise ValueError(f"{self.query_id}: this template has no keyset form")
        return self.keyset_field


# ---------------------------------------------------------------------------------------------
# QUERIES: exactly the AAP 0.3.3 "Queries issued" table
# ---------------------------------------------------------------------------------------------

# Entity enumerations exclude REMOVED rows; Q-G2b re-reads tracked campaigns a complete Q-G2 scan
# no longer returned, without a status filter, so a removal reaches the discrepancy check only
# from complete reads (safe absence). Metrics queries carry no status filter and are bounded by
# segments.date only, so spend by a campaign removed during the window still counts.
_CAMPAIGN_NOT_REMOVED: Final = "campaign.status != 'REMOVED'"
_AD_GROUP_NOT_REMOVED: Final = "ad_group.status != 'REMOVED'"
_KEYWORD_CRITERIA: Final = (
    "ad_group_criterion.type = 'KEYWORD'",
    "ad_group_criterion.status != 'REMOVED'",
)

# Plan correction: AAP 0.3.3 lists only campaign_budget.amount_micros for Q-G2. Google's
# CampaignBudget reference makes amount_micros (average daily) and total_amount_micros (lifetime,
# used only when period = CUSTOM_PERIOD) mutually exclusive, so a CUSTOM_PERIOD budget leaves
# amount_micros unset. The Google own_budget decision holds total_budget for CUSTOM_PERIOD and C0
# compares it at native precision (AAP 0.5.2, 0.5.4.4), which needs a platform value. Q-G2 and
# Q-G2b therefore also select campaign_budget.total_amount_micros, and nothing else changes; the
# row contract backend/tests/contracts/google/row_campaign.json defines totalAmountMicros to match.
_CAMPAIGN_FIELDS: Final = (
    "campaign.id",
    "campaign.name",
    "campaign.status",
    "campaign.advertising_channel_type",
    "campaign_budget.id",
    "campaign_budget.amount_micros",
    "campaign_budget.total_amount_micros",
    "campaign_budget.period",
    "campaign_budget.explicitly_shared",
)

_TEMPLATES: Final = (
    # Probe, currency, and the account time zone that sets Google's native day.
    QueryTemplate(
        query_id="Q-G1",
        resource="customer",
        select=(
            "customer.id",
            "customer.descriptive_name",
            "customer.currency_code",
            "customer.time_zone",
        ),
        row_surface="row_customer",
        strategy="single",
        keyset_field=None,
    ),
    # Snapshots, budget membership, discrepancy, utilization: a keyset scan of every
    # non-removed campaign.
    QueryTemplate(
        query_id="Q-G2",
        resource="campaign",
        select=_CAMPAIGN_FIELDS,
        row_surface="row_campaign",
        strategy="keyset",
        keyset_field="campaign.id",
        _form=_Form(fixed=(_CAMPAIGN_NOT_REMOVED,), keyset="always"),
    ),
    # Tracked campaigns a complete Q-G2 scan no longer returned, with no status filter: a
    # campaign returned as REMOVED is snapshotted as ended, one not returned raises `missing`.
    QueryTemplate(
        query_id="Q-G2b",
        resource="campaign",
        select=_CAMPAIGN_FIELDS,
        row_surface="row_campaign",
        strategy="by_ids",
        keyset_field=None,
        _form=_Form(id_list="campaign.id"),
    ),
    # Daily campaign performance for pause, scale, budget and time-pattern rules. campaign.id
    # is selected, beyond the AAP's metric list, because each daily row must be keyed to its
    # campaign; at one day and one campaign the query has one row, so it cannot overflow.
    QueryTemplate(
        query_id="Q-G3",
        resource="campaign",
        select=(
            "campaign.id",
            "segments.date",
            "metrics.cost_micros",
            "metrics.impressions",
            "metrics.clicks",
            "metrics.conversions",
            "metrics.conversions_value",
            "metrics.search_budget_lost_impression_share",
            "metrics.search_impression_share",
        ),
        row_surface="row_campaign_daily",
        strategy="dated",
        keyset_field=None,
        _form=_Form(dated=True, campaign_range=True),
    ),
    # Keyword context and the cluster gap: a keyset scan of every non-removed ad group.
    QueryTemplate(
        query_id="Q-G4",
        resource="ad_group",
        select=("ad_group.id", "ad_group.name", "ad_group.status", "campaign.id"),
        row_surface="row_ad_group",
        strategy="keyset",
        keyset_field="ad_group.id",
        _form=_Form(fixed=(_AD_GROUP_NOT_REMOVED,), keyset="always"),
    ),
    # Per-day keyword trends (K3) and coverage. At one (day, ad group) the finest form is the
    # criterion keyset, because criterion_id is unique only within its ad group.
    QueryTemplate(
        query_id="Q-G5",
        resource="keyword_view",
        select=(
            "ad_group_criterion.criterion_id",
            "ad_group.id",
            "segments.date",
            "metrics.historical_quality_score",
            "metrics.average_cpc",
            "metrics.cost_micros",
            "metrics.clicks",
            "metrics.impressions",
            "metrics.conversions",
        ),
        row_surface="row_keyword_view",
        strategy="dated",
        keyset_field="ad_group_criterion.criterion_id",
        _form=_Form(dated=True, campaign_range=True, ad_group_range=True, keyset="per_ad_group"),
    ),
    # Negatives, expansion and clusters. Search-term rows have no unique sortable id, so the
    # finest form is one (day, ad group); a capped one is stored as returned and marked
    # incomplete by partition.py.
    QueryTemplate(
        query_id="Q-G6",
        resource="search_term_view",
        select=(
            "search_term_view.search_term",
            "search_term_view.status",
            "segments.keyword.ad_group_criterion",
            "segments.keyword.info.text",
            "segments.keyword.info.match_type",
            "ad_group.id",
            "campaign.id",
            "segments.date",
            "metrics.cost_micros",
            "metrics.clicks",
            "metrics.impressions",
            "metrics.conversions",
            "metrics.conversions_value",
        ),
        row_surface="row_search_term_view",
        strategy="dated",
        keyset_field=None,
        _form=_Form(dated=True, campaign_range=True, ad_group_range=True),
    ),
    # The current keyword list, including keywords with no traffic: ad_group.id ranges, then a
    # criterion keyset inside one ad group.
    QueryTemplate(
        query_id="Q-G7",
        resource="ad_group_criterion",
        select=(
            "ad_group_criterion.criterion_id",
            "ad_group.id",
            "campaign.id",
            "ad_group_criterion.keyword.text",
            "ad_group_criterion.keyword.match_type",
            "ad_group_criterion.status",
            "ad_group_criterion.quality_info.quality_score",
        ),
        row_surface="row_ad_group_criterion",
        strategy="ad_group_ranges",
        keyset_field="ad_group_criterion.criterion_id",
        _form=_Form(fixed=_KEYWORD_CRITERIA, ad_group_range=True, keyset="per_ad_group"),
    ),
    # The counting contract B4 reads. No status filter: a removed or hidden action may still
    # have contributed to past days.
    QueryTemplate(
        query_id="Q-G8",
        resource="conversion_action",
        select=(
            "conversion_action.id",
            "conversion_action.name",
            "conversion_action.status",
            "conversion_action.include_in_conversions_metric",
            "conversion_action.counting_type",
            "conversion_action.attribution_model_settings.attribution_model",
        ),
        row_surface="row_conversion_action",
        strategy="single",
        keyset_field=None,
    ),
)

QUERIES: Final[Mapping[str, QueryTemplate]] = MappingProxyType(
    {template.query_id: template for template in _TEMPLATES}
)
"""Every GAQL template, keyed by plan id, in the order Q-G1, Q-G2, Q-G2b, Q-G3 to Q-G8."""


# ---------------------------------------------------------------------------------------------
# Inverse parsing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """A GAQL text recognised as one template and the scope it was rendered with.

    ``parse_query(QUERIES[q].render(**s)) == ParsedQuery(q, s)`` for every valid scope ``s``
    whose ``ids``, when present, is given as an ascending tuple.

    Attributes:
        query_id: Plan id of the template, a key of :data:`QUERIES`.
        scope: Scope values: ``int`` ids, ``datetime.date`` dates and, for Q-G2b, ``ids`` as an
            ascending tuple of distinct ids. Empty for Q-G1 and Q-G8.
    """

    query_id: str
    scope: Mapping[str, object]

    @property
    def template(self) -> QueryTemplate:
        """The template of :attr:`query_id`."""
        return QUERIES[self.query_id]

    @property
    def limit(self) -> int | None:
        """``LIMIT`` of the text: :data:`KEYSET_LIMIT` for keyset forms, otherwise ``None``."""
        return KEYSET_LIMIT if LAST_ID in self.scope else None

    @property
    def order_by(self) -> str | None:
        """``ORDER BY`` field of the text (the keyset field), or ``None`` without one."""
        return self.template.keyset_field if LAST_ID in self.scope else None


_KEYWORD_RE: Final = re.compile(
    r"\b(?:SELECT|FROM|WHERE|AND|BETWEEN|IN|ORDER BY|LIMIT)\b", re.IGNORECASE
)
_OPERATOR_RE: Final = re.compile(r"\s*(!=|>=|<=|=|>|<)\s*")
_QUERY_RE: Final = re.compile(
    r"SELECT (?P<select>.+?) FROM (?P<resource>[a-z][a-z0-9_]*)"
    r"(?: WHERE (?P<where>.+?))?"
    r"(?: ORDER BY [a-z][a-z0-9_.]* LIMIT [0-9]+)?"
)
_ID_PATTERN: Final = r"([0-9]{1,19})"
_DATE_PATTERN: Final = r"'([0-9]{4}-[0-9]{2}-[0-9]{2})'"
# One entry per condition kind, in rendering order: the scope keys its groups fill, then its
# pattern. A keyset condition names its field only so the text can be read; the re-render in
# parse_query proves the field is the template's own keyset field.
_CONDITION_RES: Final[tuple[tuple[tuple[str, ...], re.Pattern[str]], ...]] = (
    ((START, END), re.compile(rf"segments\.date BETWEEN {_DATE_PATTERN} AND {_DATE_PATTERN}")),
    (
        (CAMPAIGN_LO, CAMPAIGN_HI),
        re.compile(rf"campaign\.id >= {_ID_PATTERN} AND campaign\.id <= {_ID_PATTERN}"),
    ),
    (
        (AD_GROUP_LO, AD_GROUP_HI),
        re.compile(rf"ad_group\.id >= {_ID_PATTERN} AND ad_group\.id <= {_ID_PATTERN}"),
    ),
    (
        (AD_GROUP_ID, LAST_ID),
        re.compile(
            rf"ad_group\.id = {_ID_PATTERN} AND ad_group_criterion\.criterion_id > {_ID_PATTERN}"
        ),
    ),
    ((LAST_ID,), re.compile(rf"(?:campaign|ad_group)\.id > {_ID_PATTERN}")),
    ((IDS,), re.compile(r"campaign\.id IN \(([0-9]{1,19}(?:, [0-9]{1,19})*)\)")),
)
_AND: Final = " AND "


def _normalise(text: str) -> str:
    """Bring GAQL text to the spacing and keyword case :meth:`QueryTemplate.render` emits.

    Runs of whitespace collapse to one space; commas are followed by exactly one space;
    parentheses and comparison operators are spaced as rendered; GAQL keywords are upper-cased.
    No template contains a string literal with a comma, parenthesis or operator, so this never
    alters a literal.

    Args:
        text: GAQL text.

    Returns:
        The normalised text.
    """
    text = " ".join(text.split())
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s*\(\s*", " (", text)
    text = re.sub(r"\s*\)", ")", text)
    text = _OPERATOR_RE.sub(r" \1 ", text)
    text = _KEYWORD_RE.sub(lambda match: " ".join(match.group(0).upper().split()), text)
    return text.strip()


def _scope_from_where(template: QueryTemplate, where: str) -> dict[str, object]:
    """Read the scope of one template's WHERE clause.

    Args:
        template: Candidate template.
        where: The normalised text after ``WHERE``, or ``""`` without a WHERE clause.

    Returns:
        The scope, with ``int`` ids, ``datetime.date`` dates and ``ids`` as a tuple.

    Raises:
        ValueError: When the clause lacks the template's literal conditions or holds a
            condition the grammar does not know.
    """
    fixed = _AND.join(template.fixed_conditions)
    if where == fixed:
        return {}
    if fixed:
        if not where.startswith(fixed + _AND):
            raise ValueError(f"{template.query_id}: literal conditions missing")
        where = where[len(fixed) + len(_AND) :]
    scope: dict[str, object] = {}
    position = 0
    for keys, pattern in _CONDITION_RES:
        match = pattern.match(where, position)
        if match is None:
            continue
        for key, raw in zip(keys, match.groups(), strict=True):
            if key in scope:
                raise ValueError(f"{template.query_id}: scope key {key!r} given twice")
            if key in {START, END}:
                scope[key] = date.fromisoformat(raw)
            elif key == IDS:
                scope[key] = tuple(int(part) for part in raw.split(", "))
            else:
                scope[key] = int(raw)
        position = match.end()
        if position == len(where):
            return scope
        if not where.startswith(_AND, position):
            break
        position += len(_AND)
    raise ValueError(f"{template.query_id}: unrecognised WHERE condition")


def parse_query(text: str) -> ParsedQuery:
    """Recognise GAQL text as one template and recover the scope it was rendered with.

    This is the exact inverse of :meth:`QueryTemplate.render`: the text is normalised, matched
    against every template with the same ``FROM`` resource and ``SELECT`` list, its scope is
    read from the WHERE clause, and the template must render that scope back to the normalised
    text. Q-G2 and Q-G2b share their resource and fields and differ in their WHERE clause
    (``campaign.id IN (`` only in Q-G2b), so the re-render tells them apart.

    Example::

        parse_query("SELECT ... FROM campaign WHERE campaign.id IN (10, 30)")
        # ParsedQuery(query_id="Q-G2b", scope={"ids": (10, 30)})

    Args:
        text: GAQL text, typically the ``query`` of an n8n webhook request body.

    Returns:
        The template id and scope.

    Raises:
        ValueError: When the text is not a rendering of any template, including a valid query
            with a different field order, an unsorted ``IN`` list, another ``LIMIT``, or a
            scope that ``render`` would reject.
    """
    normalised = _normalise(text)
    match = _QUERY_RE.fullmatch(normalised)
    if match is None:
        raise ValueError("unrecognised GAQL: expected SELECT <fields> FROM <resource>")
    resource = match.group("resource")
    select = tuple(match.group("select").split(", "))
    where = match.group("where") or ""
    candidates = [t for t in QUERIES.values() if t.resource == resource and t.select == select]
    if not candidates:
        raise ValueError(f"unrecognised GAQL: no template selects these fields from {resource}")
    reasons: list[str] = []
    for template in candidates:
        try:
            scope = _scope_from_where(template, where)
            rendered = template.render(**scope)
        except ValueError as exc:
            reasons.append(str(exc))
            continue
        if rendered == normalised:
            return ParsedQuery(template.query_id, MappingProxyType(scope))
        reasons.append(f"{template.query_id}: text is not the canonical rendering")
    raise ValueError("unrecognised GAQL: " + "; ".join(reasons))


def query_id_for(text: str) -> str | None:
    """Return the template id of GAQL text, or ``None`` when no template renders it.

    ``simulation.payloads.identify_surface`` and the CLI's fixture capture use this to name the
    row contract of a request without failing on foreign text.

    Args:
        text: GAQL text.

    Returns:
        The plan id, such as ``"Q-G6"``, or ``None``.
    """
    try:
        return parse_query(text).query_id
    except ValueError:
        return None
