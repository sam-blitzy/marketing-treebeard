"""Seeded simulation of the three data sources, and the reserved identifiers it allocates.

Each source the product reads (Google Ads through the n8n webhook, the LinkedIn Marketing
API and HubSpot CRM) falls back on its own to seeded simulated data whenever its access is
unavailable or denied, or when ``DATA_MODE=simulated`` forces it. The simulated data is
rendered in that source's live wire contract, so the live clients and parsers run unchanged
on it and nothing gates the build (FR-11).

Submodules, each imported explicitly by its callers:

- ``generator``: ``SimulatedWorld``, ``WorldSpec``, ``DEFAULT_SPEC`` and the fault and
  platform-change specs that make the world misbehave on purpose.
- ``payloads``: contract loading and validation, ``identify_surface``, ``render`` and
  ``canonical_json``.
- ``transport``: ``mock_transport``, which serves rendered payloads through an
  ``httpx.MockTransport``.
- ``scenarios``: ``Scenario`` and the ``SCENARIOS`` catalogue that makes every rule fire in
  one case and not fire in its twin.

This module holds only constants and pure predicates: the reserved ``sim.invalid`` email
domain, the default seed and scenario name, and the reserved id ranges from which every
simulated entity is allocated. Any layer can therefore recognise a simulated id or email
without importing the world. Provenance itself is decided by the dataset a row belongs to
(``source_dataset.mode``); the predicates here only say whether a value lies in a reserved
block.

Boundaries:

- The package performs no database I/O and never imports ``ads_engine.sync`` or
  ``ads_engine.api``. Simulated data reaches the database only through the normal sync jobs
  running on the mock transport.
- This module imports nothing from ``ads_engine`` and none of its own submodules, so
  importing it never loads httpx or builds a world. It performs no I/O, draws no random
  numbers and reads no environment variables. ``Settings.mock_seed`` (``MOCK_SEED``) stays
  the runtime source of the seed; ``DEFAULT_SEED`` only mirrors its default.

Example:
    >>> from ads_engine.simulation import LINKEDIN_FORM_IDS, is_simulated_email, is_simulated_id
    >>> LINKEDIN_FORM_IDS.contains("990000101")
    True
    >>> is_simulated_id("linkedin", "urn:li:leadGenForm:990000301")
    True
    >>> is_simulated_email(" Jane.Doe@SIM.invalid ")
    True
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import combinations
from types import MappingProxyType
from typing import Final, Literal

__all__ = [
    "DEFAULT_SCENARIO",
    "DEFAULT_SEED",
    "GOOGLE_AD_GROUP_IDS",
    "GOOGLE_BUDGET_IDS",
    "GOOGLE_CAMPAIGN_IDS",
    "GOOGLE_CONVERSION_ACTION_IDS",
    "GOOGLE_CRITERION_IDS",
    "GOOGLE_ID_RANGE",
    "HUBSPOT_CONTACT_IDS",
    "HUBSPOT_DEAL_IDS",
    "HUBSPOT_HASHED_CONTACT_IDS",
    "HUBSPOT_ID_RANGE",
    "HUBSPOT_MEETING_IDS",
    "HUBSPOT_STAGE_IDS",
    "LINKEDIN_ACCOUNT_IDS",
    "LINKEDIN_CAMPAIGN_GROUP_IDS",
    "LINKEDIN_CAMPAIGN_IDS",
    "LINKEDIN_CREATIVE_IDS",
    "LINKEDIN_FORM_IDS",
    "LINKEDIN_ID_RANGE",
    "LINKEDIN_POST_IDS",
    "SIM_EMAIL_DOMAIN",
    "SIM_GOOGLE_CUSTOMER_ID",
    "IdRange",
    "is_simulated_email",
    "is_simulated_id",
]

# ---------------------------------------------------------------------------
# Seed, scenario and email domain
# ---------------------------------------------------------------------------

# RFC 2606 reserved TLD: every simulated lead email is `…@sim.invalid`, and the HubSpot
# client never sends such an address to a live portal.
SIM_EMAIL_DOMAIN: Final = "sim.invalid"

# Default world seed; mirrors the `MOCK_SEED` default. `Settings.mock_seed` is the runtime source.
DEFAULT_SEED: Final = 42

# Name under which `scenarios.SCENARIOS` holds the end-to-end world that populates every section.
DEFAULT_SCENARIO: Final = "default"


# ---------------------------------------------------------------------------
# Reserved id ranges
# ---------------------------------------------------------------------------


def _parse_decimal_id(text: str) -> int | None:
    """Parse a string of ASCII decimal digits into an integer.

    Args:
        text: Candidate identifier, such as ``"990000101"``.

    Returns:
        The integer value, or ``None`` when ``text`` is empty, holds anything other than the
        digits 0-9 (signs, whitespace, letters, non-ASCII digits) or is too long to parse.
    """
    if not (text.isascii() and text.isdecimal()):
        return None
    try:
        return int(text)
    except ValueError:
        # Longer than int() parses by default (sys.get_int_max_str_digits()); no reserved
        # range holds such a value.
        return None


@dataclass(frozen=True, slots=True)
class IdRange:
    """An inclusive range of reserved numeric identifiers.

    Simulated entities take their ids from these ranges, so a simulated id can be recognised
    without the world that produced it, and simulated HubSpot object ids can never collide
    with live ones.

    Attributes:
        start: Lowest identifier in the range.
        end: Highest identifier in the range, inclusive.

    Example:
        >>> block = IdRange(990_000_100, 990_000_999)
        >>> block.contains("990000101"), block.contains(990_001_000), block.contains("n/a")
        (True, False, False)
        >>> block.at(1)
        990000101
        >>> block.size
        900
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        """Validate the bounds.

        Raises:
            TypeError: When a bound is a ``bool``.
            ValueError: When ``start`` is negative or greater than ``end``.
        """
        if isinstance(self.start, bool) or isinstance(self.end, bool):
            raise TypeError("IdRange bounds must be integers, not booleans")
        if self.start < 0:
            raise ValueError(f"IdRange start {self.start} must be non-negative")
        if self.start > self.end:
            raise ValueError(f"IdRange start {self.start} exceeds end {self.end}")

    @property
    def size(self) -> int:
        """Number of identifiers in the range, both bounds included."""
        return self.end - self.start + 1

    def contains(self, value: int | str) -> bool:
        """Tell whether an identifier lies in the range.

        Wire formats carry int64 ids as strings, so numeric strings are accepted. This method
        never raises.

        Args:
            value: An integer id, or a string of ASCII decimal digits.

        Returns:
            True when ``value`` is an integer, or a numeric string, between ``start`` and
            ``end`` inclusive. False for booleans, for any other string (URNs included;
            see ``is_simulated_id``) and for values outside the range.
        """
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            number = value
        else:
            parsed = _parse_decimal_id(value)
            if parsed is None:
                return False
            number = parsed
        return self.start <= number <= self.end

    def at(self, offset: int) -> int:
        """Return the identifier ``offset`` places after ``start``.

        Generators allocate ids deterministically as ``RANGE.at(index)``.

        Args:
            offset: Zero-based position within the range.

        Returns:
            ``start + offset``.

        Raises:
            ValueError: When ``offset`` is negative or the result would exceed ``end``.
        """
        if offset < 0:
            raise ValueError(f"IdRange offset {offset} must be non-negative")
        value = self.start + offset
        if value > self.end:
            raise ValueError(
                f"IdRange offset {offset} exceeds the range [{self.start}, {self.end}] "
                f"of {self.size} ids"
            )
        return value


# LinkedIn: one 9-digit block starting 990, the block whose URNs config/content_assets.yaml ships.
# Every simulated LinkedIn id comes from one of the non-overlapping sub-ranges below.
LINKEDIN_ID_RANGE: Final = IdRange(990_000_000, 990_999_999)
# Ad accounts (urn:li:sponsoredAccount:{id}); the default world uses 990000001 and 990000002.
LINKEDIN_ACCOUNT_IDS: Final = IdRange(990_000_001, 990_000_099)
# Lead forms (urn:li:leadGenForm:{id}); holds the config forms 990000101/102/201/301.
LINKEDIN_FORM_IDS: Final = IdRange(990_000_100, 990_000_999)
# Creatives (urn:li:sponsoredCreative:{id}); holds the config override creative 990001301.
LINKEDIN_CREATIVE_IDS: Final = IdRange(990_001_000, 990_009_999)
# Campaigns (urn:li:sponsoredCampaign:{id}).
LINKEDIN_CAMPAIGN_IDS: Final = IdRange(990_010_000, 990_019_999)
# Campaign groups (urn:li:sponsoredCampaignGroup:{id}).
LINKEDIN_CAMPAIGN_GROUP_IDS: Final = IdRange(990_020_000, 990_029_999)
# Posts that creatives reference for headline and intro text.
LINKEDIN_POST_IDS: Final = IdRange(990_030_000, 990_039_999)

# Google: the simulated customer id, 10 digits with no dashes, as the webhook body requires.
SIM_GOOGLE_CUSTOMER_ID: Final = "9900000001"
# Google: one 12-digit block starting 990 for every other simulated Google id.
GOOGLE_ID_RANGE: Final = IdRange(990_000_000_000, 990_999_999_999)
# Campaigns (campaign.id).
GOOGLE_CAMPAIGN_IDS: Final = IdRange(990_000_000_000, 990_099_999_999)
# Campaign budgets (campaign_budget.id), own and explicitly shared.
GOOGLE_BUDGET_IDS: Final = IdRange(990_100_000_000, 990_199_999_999)
# Ad groups (ad_group.id).
GOOGLE_AD_GROUP_IDS: Final = IdRange(990_200_000_000, 990_299_999_999)
# Keyword criteria (ad_group_criterion.criterion_id), allocated account-wide.
GOOGLE_CRITERION_IDS: Final = IdRange(990_300_000_000, 990_399_999_999)
# Conversion actions (conversion_action.id).
GOOGLE_CONVERSION_ACTION_IDS: Final = IdRange(990_400_000_000, 990_499_999_999)

# HubSpot: one 16-digit object-id block, orders of magnitude above live HubSpot object ids, so
# simulated contacts, meetings and deals can never collide with live ones. The values exceed
# 2**53; like HubSpot's own wire format, they are carried as strings outside Python.
HUBSPOT_ID_RANGE: Final = IdRange(9_900_000_000_000_000, 9_900_999_999_999_999)
# Contacts of simulated people (companion and simulated primary CRM datasets).
HUBSPOT_CONTACT_IDS: Final = IdRange(9_900_000_000_000_000, 9_900_099_999_999_999)
# Seeded contacts answering real lead emails; each id is derived from the email's SHA-256.
HUBSPOT_HASHED_CONTACT_IDS: Final = IdRange(9_900_100_000_000_000, 9_900_199_999_999_999)
# Meetings.
HUBSPOT_MEETING_IDS: Final = IdRange(9_900_200_000_000_000, 9_900_299_999_999_999)
# Deals.
HUBSPOT_DEAL_IDS: Final = IdRange(9_900_300_000_000_000, 9_900_399_999_999_999)
# Numeric ids of simulated custom lifecycle stages (custom stages have numeric ids). These are
# `lifecyclestage` option values, a namespace separate from CRM object ids, so this range lies
# outside HUBSPOT_ID_RANGE by design and `is_simulated_id("hubspot", …)` does not consult it.
HUBSPOT_STAGE_IDS: Final = IdRange(990_100_000, 990_100_999)


type _RangeFamily = tuple[str, IdRange, tuple[tuple[str, IdRange], ...]]

# Object-id blocks and their sub-ranges, checked at import by _check_disjoint(). Each family
# is one id namespace; families of different sources may overlap numerically. HUBSPOT_STAGE_IDS
# is a single-range namespace with no sub-ranges, so it has nothing to check here.
_RANGE_FAMILIES: Final[tuple[_RangeFamily, ...]] = (
    (
        "LINKEDIN_ID_RANGE",
        LINKEDIN_ID_RANGE,
        (
            ("LINKEDIN_ACCOUNT_IDS", LINKEDIN_ACCOUNT_IDS),
            ("LINKEDIN_FORM_IDS", LINKEDIN_FORM_IDS),
            ("LINKEDIN_CREATIVE_IDS", LINKEDIN_CREATIVE_IDS),
            ("LINKEDIN_CAMPAIGN_IDS", LINKEDIN_CAMPAIGN_IDS),
            ("LINKEDIN_CAMPAIGN_GROUP_IDS", LINKEDIN_CAMPAIGN_GROUP_IDS),
            ("LINKEDIN_POST_IDS", LINKEDIN_POST_IDS),
        ),
    ),
    (
        "GOOGLE_ID_RANGE",
        GOOGLE_ID_RANGE,
        (
            ("GOOGLE_CAMPAIGN_IDS", GOOGLE_CAMPAIGN_IDS),
            ("GOOGLE_BUDGET_IDS", GOOGLE_BUDGET_IDS),
            ("GOOGLE_AD_GROUP_IDS", GOOGLE_AD_GROUP_IDS),
            ("GOOGLE_CRITERION_IDS", GOOGLE_CRITERION_IDS),
            ("GOOGLE_CONVERSION_ACTION_IDS", GOOGLE_CONVERSION_ACTION_IDS),
        ),
    ),
    (
        "HUBSPOT_ID_RANGE",
        HUBSPOT_ID_RANGE,
        (
            ("HUBSPOT_CONTACT_IDS", HUBSPOT_CONTACT_IDS),
            ("HUBSPOT_HASHED_CONTACT_IDS", HUBSPOT_HASHED_CONTACT_IDS),
            ("HUBSPOT_MEETING_IDS", HUBSPOT_MEETING_IDS),
            ("HUBSPOT_DEAL_IDS", HUBSPOT_DEAL_IDS),
        ),
    ),
)


def _check_disjoint(families: tuple[_RangeFamily, ...] = _RANGE_FAMILIES) -> None:
    """Check that every sub-range lies inside its parent and no two sub-ranges overlap.

    Runs once at import; it is pure and costs a few dozen comparisons. It raises explicitly
    rather than through ``assert``, so ``python -O`` cannot strip the check.

    Args:
        families: ``(parent name, parent range, ((sub-range name, sub-range), ...))`` tuples;
            defaults to the reserved ranges of this module.

    Raises:
        AssertionError: Naming every sub-range that leaves its parent and every overlapping
            pair of sub-ranges.
    """
    problems: list[str] = []
    for parent_name, parent, children in families:
        for name, child in children:
            if child.start < parent.start or child.end > parent.end:
                problems.append(
                    f"{name} [{child.start}, {child.end}] lies outside "
                    f"{parent_name} [{parent.start}, {parent.end}]"
                )
        for (left_name, left), (right_name, right) in combinations(children, 2):
            if left.start <= right.end and right.start <= left.end:
                problems.append(f"{left_name} overlaps {right_name} within {parent_name}")
    if problems:
        raise AssertionError(
            "Reserved simulation id ranges are inconsistent: " + "; ".join(problems)
        )


_check_disjoint()


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def is_simulated_email(email: str) -> bool:
    """Tell whether an email address belongs to a simulated person.

    The address is normalised the way lead emails are before hashing (trimmed and lowercased)
    and is never logged or stored.

    Args:
        email: An email address, in any case and with surrounding whitespace allowed.

    Returns:
        True when the domain after the last ``@`` equals ``SIM_EMAIL_DOMAIN``. False when
        the address has no ``@`` or another domain, a subdomain such as ``x.sim.invalid``
        included.

    Example:
        >>> is_simulated_email("lead-0001@sim.invalid"), is_simulated_email("ann@example.com")
        (True, False)
    """
    _local, separator, domain = email.strip().lower().rpartition("@")
    return bool(separator) and domain == SIM_EMAIL_DOMAIN


def _linkedin_plain_id(value: str) -> str:
    """Return the id of a simple LinkedIn URN, or a non-URN value unchanged.

    Args:
        value: A plain id such as ``"990000101"`` or a URN such as
            ``"urn:li:leadGenForm:990000101"``.

    Returns:
        The trailing ``<id>`` segment of ``urn:li:<type>:<id>``; ``""`` for any other URN,
        including compound URNs such as ``urn:li:versionedLeadGenForm:(…,2)``, which callers
        reduce to their canonical URN first; otherwise ``value`` itself.
    """
    parts = value.split(":")
    if len(parts) == 4 and parts[0] == "urn" and parts[1] == "li" and parts[2]:
        return parts[3]
    if value.startswith("urn:"):
        return ""
    return value


def _is_simulated_google_id(value: int | str) -> bool:
    """Tell whether a Google id is the simulated customer id or lies in ``GOOGLE_ID_RANGE``."""
    if isinstance(value, bool):
        return False
    return GOOGLE_ID_RANGE.contains(value) or str(value) == SIM_GOOGLE_CUSTOMER_ID


def _is_simulated_linkedin_id(value: int | str) -> bool:
    """Tell whether a LinkedIn id or simple URN lies in ``LINKEDIN_ID_RANGE``."""
    if isinstance(value, str):
        return LINKEDIN_ID_RANGE.contains(_linkedin_plain_id(value))
    return LINKEDIN_ID_RANGE.contains(value)


# Dispatch by source name; a mapping rather than an if-chain, so an unexpected runtime value
# reaches an explicit error instead of code the type checker considers unreachable.
_ID_PREDICATES: Final[Mapping[str, Callable[[int | str], bool]]] = MappingProxyType(
    {
        "google": _is_simulated_google_id,
        "linkedin": _is_simulated_linkedin_id,
        "hubspot": HUBSPOT_ID_RANGE.contains,
    }
)


def is_simulated_id(source: Literal["google", "linkedin", "hubspot"], value: int | str) -> bool:
    """Tell whether a source identifier lies in that source's reserved simulated block.

    Args:
        source: The source the identifier comes from.
        value: The identifier as an integer or string. Google: a customer id or any
            ``GOOGLE_*`` id. LinkedIn: a plain id or a simple URN ``urn:li:<type>:<id>``,
            whose trailing id segment is checked. HubSpot: a CRM object id (contact, meeting
            or deal); lifecycle stage ids are a separate namespace (``HUBSPOT_STAGE_IDS``).

    Returns:
        True when ``value`` lies in the source's reserved block; False otherwise, including
        for malformed values. The id blocks of different sources overlap numerically, so the
        answer is meaningful only for the named source.

    Raises:
        ValueError: When ``source`` is not ``"google"``, ``"linkedin"`` or ``"hubspot"``.

    Example:
        >>> is_simulated_id("google", "9900000001"), is_simulated_id("hubspot", "51")
        (True, False)
    """
    predicate = _ID_PREDICATES.get(source)
    if predicate is None:
        raise ValueError(f"Unknown source {source!r}; expected 'google', 'linkedin' or 'hubspot'")
    return predicate(value)
