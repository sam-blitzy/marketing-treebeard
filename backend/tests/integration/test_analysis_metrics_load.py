"""DB-backed tests for the SQL half of ``ads_engine.analysis.metrics``.

AAP 0.5.1 ("Metrics and statistics": ``aggregate(ctx, grain)``, window aggregation as SQL
group-bys), 0.3.3 (active-dataset filter; the effective CRM dataset, which is the companion
while LinkedIn is simulated; the Google-to-CRM link on HubSpot's active primary), 0.5.4.1
(one calendar-date range on each source's native dates; leads within
``[start 00:00 UTC, end + 1 day 00:00 UTC)``; budget snapshots as observations; deals created
from window start to run time) and 0.11.1 (absent values are ``None``, never 0).

The tests drive :class:`~ads_engine.analysis.metrics.QueryScope`,
:func:`~ads_engine.analysis.metrics.aggregate_sql` for every grain and
:func:`~ads_engine.analysis.metrics.load_context` with every loader it calls: each grain's
rows, sums and order; the pure ``aggregate(ctx, grain)`` reading every grain of a loaded
context back exactly as ``aggregate_sql`` returns it; dataset isolation (no row of a retired
dataset, or of a dataset the context does not read, ever appears); the companion CRM dataset
a simulated LinkedIn dataset
reads; the Google-to-CRM link on HubSpot's primary; the shared CRM load while LinkedIn is
live, whose Google discovery runs only under a verified Google link; stored CRM bindings that
are valid, undecodable or never verified; unavailable values
that stay ``None``; and every path of a source without an active dataset.

The last tests hold ``load_context`` to the conventions
``simulation.scenarios.context_from_world`` documents as its pure stand-in for "the sync jobs
store the world, then ``load_context`` reads it back": window deals created in
[window start, run time], keyword snapshots trimmed to the first and last inside Google's
window, the discrepancy shape from the latest decision, budgets read with an empty name, and
search terms, segments, CRM associations, fetches, window deals and Google-touch contacts
listed in the scenario's code-point order whatever the database's default collation.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test starts from empty tables of that
schema. An unreachable database fails the database tests; it never skips them.
"""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from ads_engine.analysis import metrics
from ads_engine.analysis.context import (
    AccountInfo,
    AdGroupInfo,
    AnalysisContext,
    AssociationFetchInfo,
    BudgetInfo,
    CampaignGroupInfo,
    CampaignInfo,
    CampaignStatus,
    ContactInfo,
    ConversionActionInfo,
    CreativeInfo,
    CreativeTotals,
    DatasetRef,
    DayFact,
    DealInfo,
    DecisionInfo,
    DiscrepancyInfo,
    FeedbackInfo,
    Freshness,
    KeywordDay,
    KeywordInfo,
    LeadInfo,
    MeetingInfo,
    Observation,
    RateBaseline,
    SearchTermRow,
    SegmentRow,
    Source,
    objective_group,
)
from ads_engine.analysis.metrics import (
    GRAINS,
    CampaignDay,
    QueryScope,
    aggregate,
    aggregate_sql,
    load_context,
)
from ads_engine.analysis.thresholds import (
    CrmBindings,
    GoogleCrmBinding,
    ResolvedBinding,
    Thresholds,
)
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo
from ads_engine.db.base import Base
from ads_engine.db.models import (
    AdAccount,
    AdGroup,
    AnalysisRun,
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
    Recommendation,
    RecommendationFeedback,
    SourceDataset,
)

pytestmark = pytest.mark.integration

type Sessions = async_sessionmaker[AsyncSession]

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The run time of every context load."""

GOOGLE_TZ = "America/New_York"
"""The Google customer's time zone, Google's native day boundary (UTC-4 in this window)."""

START = date(2026, 9, 9)
END = date(2026, 10, 6)
"""The default 28-day window at :data:`NOW`: yesterday in both New York and UTC is its end."""

MID = date(2026, 9, 20)
BEFORE = START - timedelta(days=1)
AFTER = END + timedelta(days=1)
BASELINE_START = END - timedelta(days=89)
"""The first date of the shipped 90-day ``k1.baseline_days`` span ending on :data:`END`."""

UTC_LOWER = datetime(2026, 9, 9, tzinfo=UTC)
UTC_UPPER = datetime(2026, 10, 7, tzinfo=UTC)
"""``window.utc_bounds()``: leads and the window deals' start."""

NY_LOWER = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
NY_UPPER = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
"""The window's start and end + 1 day at midnight in :data:`GOOGLE_TZ`, as UTC instants:
the Google-native range of keyword snapshots and Google-touch contacts."""

OBS_LOWER = datetime(2026, 9, 7, tzinfo=UTC)
OBS_UPPER = datetime(2026, 10, 9, tzinfo=UTC)
"""The observation span ``[utc(start) - 2 days, utc(end + 1 day) + 2 days)``."""

MID_06 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
MID_NOON = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MICRO = timedelta(microseconds=1)

FORM_A = "urn:li:leadGenForm:900"
FORM_B = "urn:li:leadGenForm:901"
URN_1 = "urn:li:sponsoredCreative:8001"
URN_2 = "urn:li:sponsoredCreative:8002"
URN_3 = "urn:li:sponsoredCreative:8003"

VALID_BINDINGS = CrmBindings(
    mql=ResolvedBinding(
        available=True, property="date_first_meeting_was_booked", label="First meeting booked"
    ),
    ql=ResolvedBinding(
        available=True,
        property="hs_v2_date_entered_1406943076",
        stage_id="1406943076",
        label="Qualified Lead",
    ),
    google=GoogleCrmBinding(
        available=True,
        campaign_property="google_campaign_id",
        touch_date_property="google_touch_date",
    ),
    verified_at=NOW - timedelta(hours=2),
)
"""Bindings verified against the live portal: MQL, QL and the Google link available."""

UNDECODABLE_BINDINGS: dict[str, Any] = {**VALID_BINDINGS.to_json(), "verified_at": "yesterday"}
"""A stored ``crm_bindings`` document that no longer decodes (an invalid timestamp)."""

GOOGLE_LINK_UNAVAILABLE = CrmBindings(
    mql=VALID_BINDINGS.mql,
    ql=VALID_BINDINGS.ql,
    google=GoogleCrmBinding(
        available=False, reason="property 'google_touch_date' not found on contacts"
    ),
    verified_at=VALID_BINDINGS.verified_at,
)
"""Bindings re-verified after the touch-date property was deleted: MQL and QL only."""


def _d(value: str | int) -> Decimal:
    """Return ``value`` as a ``Decimal``."""
    return Decimal(value)


def _email_hash(local: str) -> str:
    """Return the SHA-256 hex digest of a reserved ``sim.invalid`` address."""
    return hashlib.sha256(f"{local}@sim.invalid".encode()).hexdigest()


# ---------------------------------------------------------------------------------------
# Configuration, window and log capture
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    """The shipped ``config/*.yaml``."""
    return Thresholds.load(CONFIG_DIR)


@pytest.fixture(scope="module")
def window(thresholds: Thresholds) -> AnalysisWindow:
    """The configured default window at :data:`NOW` with Google days in New York."""
    return AnalysisWindow.default(thresholds, SourceDayInfo(GOOGLE_TZ), now=NOW)


@dataclass(slots=True)
class _Log:
    """Records the structured log calls ``metrics`` makes, in place of its logger."""

    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def info(self, event: str, **fields: Any) -> None:
        """Record an info event."""
        self.events.append(("info", event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        """Record a warning event."""
        self.events.append(("warning", event, fields))

    def named(self, event: str) -> list[tuple[str, dict[str, Any]]]:
        """Return the level and fields of every recorded ``event``."""
        return [(level, fields) for level, name, fields in self.events if name == event]


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Log:
    """Replace the module logger with a recorder for this test."""
    recorder = _Log()
    monkeypatch.setattr(metrics, "_log", recorder)
    return recorder


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the analysis metrics load tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _connect_args(schema: str) -> dict[str, str]:
    """Pin every connection to ``schema`` alone, in UTC like the application's sessions."""
    return {"options": f"-c search_path={schema} -c TimeZone=UTC"}


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"metrics_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
async def engine(database: tuple[URL, str]) -> AsyncIterator[AsyncEngine]:
    """Yield an engine on the private schema, whose tables this test starts from empty."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with engine.begin() as connection:
            preparer = connection.dialect.identifier_preparer
            tables = ", ".join(preparer.quote(table.name) for table in Base.metadata.sorted_tables)
            await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> Sessions:
    """Return a session factory on this test's engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------------------


async def _put[T](session: AsyncSession, row: T) -> T:
    """Insert one row and flush, so its generated id is known."""
    session.add(row)
    await session.flush()
    return row


async def _dataset(
    session: AsyncSession,
    source: str,
    mode: str,
    state: str,
    *,
    parent: int | None = None,
    bindings: dict[str, Any] | None = None,
) -> int:
    """Insert a dataset: a primary one, or a simulated HubSpot companion of ``parent``."""
    row = await _put(
        session,
        SourceDataset(
            source=source,
            mode=mode,
            state=state,
            role="primary" if parent is None else "companion",
            parent_dataset_id=parent,
            reason="credentials not configured" if mode == "simulated" else None,
            activated_at=NOW - timedelta(days=40),
            retired_at=NOW - timedelta(days=5) if state == "retired" else None,
            crm_bindings=bindings,
        ),
    )
    return row.id


async def _google_day(
    session: AsyncSession,
    dataset_id: int,
    campaign_id: int,
    day: date,
    cost: str,
    impressions: int,
    clicks: int,
    conversions: str | None,
    value: str | None = None,
    lost_is: str | None = None,
    share: str | None = None,
) -> None:
    """Insert one ``fact_campaign_daily`` row."""
    await _put(
        session,
        FactCampaignDaily(
            source="google",
            dataset_id=dataset_id,
            campaign_id=campaign_id,
            date=day,
            cost=_d(cost),
            impressions=impressions,
            clicks=clicks,
            conversions=None if conversions is None else _d(conversions),
            conversions_value=None if value is None else _d(value),
            budget_lost_is=None if lost_is is None else _d(lost_is),
            impression_share=None if share is None else _d(share),
            fetched_at=NOW,
        ),
    )


async def _creative_day(
    session: AsyncSession,
    dataset_id: int,
    creative_id: int,
    day: date,
    *,
    cost: str | None = None,
    impressions: int | None = None,
    clicks: int | None = None,
    conversions: str | None = None,
    value: str | None = None,
    form_opens: int | None = None,
    leads: int | None = None,
    landing_page_clicks: int | None = None,
    website_conversions: int | None = None,
) -> None:
    """Insert one ``fact_creative_daily`` row; an omitted metric is unreported (``NULL``)."""
    await _put(
        session,
        FactCreativeDaily(
            source="linkedin",
            dataset_id=dataset_id,
            creative_id=creative_id,
            date=day,
            cost=None if cost is None else _d(cost),
            impressions=impressions,
            clicks=clicks,
            conversions=None if conversions is None else _d(conversions),
            conversions_value=None if value is None else _d(value),
            form_opens=form_opens,
            leads=leads,
            landing_page_clicks=landing_page_clicks,
            website_conversions=website_conversions,
            fetched_at=NOW,
        ),
    )


async def _search_term(
    session: AsyncSession,
    dataset_id: int,
    ad_group_id: int,
    campaign_id: int,
    term: str,
    criterion: str,
    day: date,
    status: str,
    keyword: tuple[str, str] | None,
    metrics_row: tuple[str, int, int, str | None, str | None],
) -> None:
    """Insert one ``fact_search_term_daily`` row: ``metrics_row`` is cost, clicks,
    impressions, conversions and conversion value."""
    cost, clicks, impressions, conversions, value = metrics_row
    await _put(
        session,
        FactSearchTermDaily(
            source="google",
            dataset_id=dataset_id,
            ad_group_id=ad_group_id,
            campaign_id=campaign_id,
            date=day,
            search_term=term,
            term_status=status,
            matched_criterion_id=criterion,
            matched_keyword_text=None if keyword is None else keyword[0],
            matched_match_type=None if keyword is None else keyword[1],
            cost=_d(cost),
            impressions=impressions,
            clicks=clicks,
            conversions=None if conversions is None else _d(conversions),
            conversions_value=None if value is None else _d(value),
            fetched_at=NOW,
        ),
    )


async def _snapshot(
    session: AsyncSession,
    source: str,
    dataset_id: int,
    entity_type: str,
    entity_id: int,
    captured_at: datetime,
    **columns: Any,
) -> None:
    """Insert one ``entity_snapshot`` row; ``columns`` fills the observed values."""
    columns.setdefault("payload", {})
    await _put(
        session,
        EntitySnapshot(
            source=source,
            dataset_id=dataset_id,
            entity_type=entity_type,
            entity_id=entity_id,
            captured_at=captured_at,
            **columns,
        ),
    )


async def _contact(
    session: AsyncSession, dataset_id: int, hs_id: str, **columns: Any
) -> CrmContact:
    """Insert one ``crm_contact`` row of ``dataset_id``."""
    return await _put(
        session,
        CrmContact(source="hubspot", dataset_id=dataset_id, hs_id=hs_id, updated_at=NOW, **columns),
    )


async def _deal(
    session: AsyncSession,
    dataset_id: int,
    hs_id: str,
    created_at: datetime | None,
    amount: str | None = None,
) -> None:
    """Insert one ``crm_deal`` row of ``dataset_id``."""
    await _put(
        session,
        CrmDeal(
            source="hubspot",
            dataset_id=dataset_id,
            hs_id=hs_id,
            created_at=created_at,
            stage="appointmentscheduled",
            pipeline="default",
            amount=None if amount is None else _d(amount),
        ),
    )


async def _associate(
    session: AsyncSession, dataset_id: int, from_type: str, from_id: str, to_type: str, to_id: str
) -> None:
    """Insert one ``crm_association`` row of ``dataset_id``."""
    await _put(
        session,
        CrmAssociation(
            source="hubspot",
            dataset_id=dataset_id,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
        ),
    )


async def _fetch(
    session: AsyncSession,
    dataset_id: int,
    key: tuple[str, str, str],
    status: str,
    target: str | None,
    missing: tuple[str, ...] = (),
) -> None:
    """Insert one ``crm_association_fetch`` row: ``key`` is (object type, id, target type)."""
    object_type, object_id, to_type = key
    await _put(
        session,
        CrmAssociationFetch(
            source="hubspot",
            dataset_id=dataset_id,
            object_type=object_type,
            object_id=object_id,
            to_type=to_type,
            status=status,
            target_read_status=target,
            missing_target_ids=list(missing),
            fetched_at=NOW - timedelta(hours=1),
            error="HTTP 500" if status == "error" else None,
        ),
    )


async def _match(
    session: AsyncSession,
    lead_id: int,
    crm_dataset_id: int,
    status: str,
    *,
    contact: int | None = None,
    read: str | None = None,
    reason: str | None = None,
) -> None:
    """Insert one ``lead_match`` row of a lead in ``crm_dataset_id``."""
    await _put(
        session,
        LeadMatch(
            lead_id=lead_id,
            crm_dataset_id=crm_dataset_id,
            status=status,
            attempts=1,
            next_attempt_at=None,
            last_checked_at=NOW - timedelta(hours=2),
            contact_id=contact,
            contact_read_status=read,
            reason=reason,
        ),
    )


async def _lead(
    session: AsyncSession,
    dataset_id: int,
    external_id: str,
    submitted_at: datetime,
    campaign_id: int,
    creative_id: int | None,
    form: str = FORM_A,
    version: int = 3,
) -> int:
    """Insert one LinkedIn ``lead`` row with an email hash; return its id."""
    row = await _put(
        session,
        Lead(
            source="linkedin",
            dataset_id=dataset_id,
            external_id=external_id,
            submitted_at=submitted_at,
            campaign_id=campaign_id,
            creative_id=creative_id,
            lead_form_urn=form,
            lead_form_version=version,
            email_sha256=_email_hash(external_id),
        ),
    )
    return row.id


async def _decision(
    session: AsyncSession,
    source: str,
    dataset_id: int,
    subject: tuple[str, int],
    shape: str,
    decided_at: datetime,
    *,
    status: str | None = None,
    daily: str | None = None,
    total: str | None = None,
    budget_id: int | None = None,
    tombstone: bool = False,
    origin: str = "user",
) -> None:
    """Insert one ``decision`` row: the complete expected state of ``subject``."""
    await _put(
        session,
        Decision(
            source=source,
            dataset_id=dataset_id,
            subject_type=subject[0],
            subject_id=subject[1],
            shape=shape,
            status=status,
            daily_budget=None if daily is None else _d(daily),
            total_budget=None if total is None else _d(total),
            budget_id=budget_id,
            tombstone=tombstone,
            decided_at=decided_at,
            decided_by="admin@example.com",
            origin=origin,
            patch={},
        ),
    )


async def _discrepancy(
    session: AsyncSession,
    source: str,
    dataset_id: int,
    subject: tuple[str, int],
    kind: str,
    detected_at: datetime,
    *,
    decided: dict[str, Any] | None,
    platform: dict[str, Any] | None,
    state: str = "open",
) -> int:
    """Insert one ``discrepancy`` row of ``subject``; return its id."""
    resolved = state == "resolved"
    row = await _put(
        session,
        Discrepancy(
            source=source,
            dataset_id=dataset_id,
            subject_type=subject[0],
            subject_id=subject[1],
            kind=kind,
            decided_value=decided,
            platform_value=platform,
            detected_at=detected_at,
            state=state,
            resolved_at=detected_at + timedelta(hours=1) if resolved else None,
            resolution="platform_correct" if resolved else None,
        ),
    )
    return row.id


async def _freshness(
    session: AsyncSession,
    source: str,
    dataset_id: int,
    name: str,
    completeness: str = "complete",
    *,
    covered_from: date | None = date(2026, 4, 10),
    reason: str | None = None,
) -> None:
    """Insert one ``dataset_freshness`` row covering through :data:`END`."""
    await _put(
        session,
        DatasetFreshness(
            source=source,
            dataset_id=dataset_id,
            dataset=name,
            last_attempt_at=NOW - timedelta(hours=1),
            last_success_at=NOW - timedelta(hours=1),
            covered_from=covered_from,
            covered_through=END,
            completeness=completeness,
            reason=reason,
        ),
    )


# ---------------------------------------------------------------------------------------
# The main world: live Google and HubSpot, simulated LinkedIn with its companion CRM
# dataset, and a retired copy of every source whose rows must never appear
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class _World:
    """The ids of the main world's rows that the assertions name."""

    g: int = 0
    g_old: int = 0
    li: int = 0
    c: int = 0
    li_old: int = 0
    c_old: int = 0
    h: int = 0
    h_old: int = 0
    acct_g: int = 0
    acct_g2: int = 0
    acct_l: int = 0
    group: int = 0
    b_shared: int = 0
    b_own: int = 0
    b_orphan: int = 0
    b_cad: int = 0
    camp_g1: int = 0
    camp_g2: int = 0
    camp_g3: int = 0
    camp_g4: int = 0
    camp_l1: int = 0
    camp_l2: int = 0
    camp_old: int = 0
    ag: int = 0
    kw1: int = 0
    kw2: int = 0
    ca1: int = 0
    ca2: int = 0
    cr1: int = 0
    cr2: int = 0
    cr3: int = 0
    lead_a: int = 0
    lead_b: int = 0
    lead_c: int = 0
    lead_f: int = 0
    d_status: int = 0
    d_untracked_own: int = 0
    d_untracked_member: int = 0
    d_budget: int = 0
    d_missing_budget: int = 0
    d_unknown_campaign: int = 0
    d_unknown_budget: int = 0
    d_linkedin: int = 0
    kw1_snapshots: list[tuple[datetime, int]] = field(default_factory=list)
    deal_instants: dict[str, datetime | None] = field(default_factory=dict)


async def _seed_google(session: AsyncSession, w: _World) -> None:
    """Seed the active Google dataset and a retired one with clashing rows."""
    g = w.g
    acct = await _put(
        session,
        AdAccount(
            source="google",
            dataset_id=g,
            external_id="1234567890",
            name=None,
            currency="USD",
            timezone=GOOGLE_TZ,
        ),
    )
    acct2 = await _put(
        session,
        AdAccount(
            source="google",
            dataset_id=g,
            external_id="2223334444",
            name="Canada",
            currency="CAD",
            timezone="America/Toronto",
        ),
    )
    w.acct_g, w.acct_g2 = acct.id, acct2.id
    budgets: dict[str, int] = {}
    for key, external, amount, period, shared in (
        ("shared", "9001", 50, "DAILY", True),
        ("own", "9002", 20, "DAILY", False),
        ("orphan", "9003", None, "CUSTOM_PERIOD", False),
        ("cad", "9004", 30, "DAILY", False),
    ):
        row = await _put(
            session,
            Budget(
                source="google",
                dataset_id=g,
                external_id=external,
                amount=None if amount is None else _d(amount),
                amount_micros=None if amount is None else amount * 1_000_000,
                period=period,
                explicitly_shared=shared,
            ),
        )
        budgets[key] = row.id
    w.b_shared, w.b_own, w.b_orphan, w.b_cad = (
        budgets["shared"],
        budgets["own"],
        budgets["orphan"],
        budgets["cad"],
    )
    campaigns: dict[str, int] = {}
    for key, account, external, name, status, raw, budget, daily in (
        ("g1", acct.id, "111", "Search Brand", "active", "ENABLED", w.b_shared, 50),
        ("g2", acct.id, "112", "Search Generic", "active", "ENABLED", w.b_shared, 50),
        ("g3", acct.id, "113", "Search Own", "paused", "PAUSED", w.b_own, 20),
        ("g4", acct2.id, "114", "Canada Search", "active", "ENABLED", w.b_cad, 30),
    ):
        campaign_row = await _put(
            session,
            Campaign(
                source="google",
                dataset_id=g,
                account_id=account,
                external_id=external,
                name=name,
                status=status,
                raw_status=raw,
                objective_or_channel="SEARCH",
                budget_id=budget,
                daily_budget=_d(daily),
                budget_period="DAILY",
            ),
        )
        campaigns[key] = campaign_row.id
    w.camp_g1, w.camp_g2, w.camp_g3, w.camp_g4 = (
        campaigns["g1"],
        campaigns["g2"],
        campaigns["g3"],
        campaigns["g4"],
    )
    group = await _put(
        session,
        AdGroup(
            source="google",
            dataset_id=g,
            campaign_id=w.camp_g1,
            external_id="5001",
            name="Brand terms",
            status="ENABLED",
        ),
    )
    w.ag = group.id
    kw1 = await _put(
        session,
        Keyword(
            source="google",
            dataset_id=g,
            ad_group_id=group.id,
            criterion_id="7001",
            text="acme software",
            match_type="EXACT",
            status="ENABLED",
            quality_score=7,
        ),
    )
    kw2 = await _put(
        session,
        Keyword(
            source="google",
            dataset_id=g,
            ad_group_id=group.id,
            criterion_id="7002",
            text="acme tools",
            match_type="PHRASE",
            status="ENABLED",
            quality_score=None,
        ),
    )
    w.kw1, w.kw2 = kw1.id, kw2.id
    ca1 = await _put(
        session,
        ConversionAction(
            source="google",
            dataset_id=g,
            external_id="801",
            name="Demo request",
            status="ENABLED",
            include_in_conversions_metric=True,
            counting_type="ONE_PER_CLICK",
            attribution_model="GOOGLE_ADS_LAST_CLICK",
        ),
    )
    ca2 = await _put(
        session,
        ConversionAction(
            source="google",
            dataset_id=g,
            external_id="802",
            name="Newsletter",
            status="HIDDEN",
            include_in_conversions_metric=False,
            counting_type=None,
            attribution_model=None,
        ),
    )
    w.ca1, w.ca2 = ca1.id, ca2.id

    # Daily facts: three window days, the day before the window (in the K1 span only), the
    # first and the day before the K1 span, and today (after the window end).
    for day, cost, impressions, clicks, conversions, value, lost_is, share in (
        (START, "10.5", 100, 10, "1.5", "30", "0.12", "0.8"),
        (MID, "5", 50, 4, None, None, None, None),
        (END, "7", 70, 7, "0", "0", None, None),
        (BEFORE, "3", 30, 3, "1", None, None, None),
        (BASELINE_START, "2", 20, 20, "2", None, None, None),
        (BASELINE_START - timedelta(days=1), "9", 1000, 1000, "100", None, None, None),
        (AFTER, "4", 40, 40, "4", None, None, None),
    ):
        await _google_day(
            session,
            g,
            w.camp_g1,
            day,
            cost,
            impressions,
            clicks,
            conversions,
            value,
            lost_is,
            share,
        )
    await _google_day(session, g, w.camp_g3, MID, "2", 10, 1, "0")
    for day, cost, clicks, impressions, conversions, quality, cpc in (
        (START, "2", 2, 20, "1", 7, "1.0"),
        (END, "3", 1, 10, None, None, None),
        (BEFORE, "9", 9, 90, "9", 9, "1"),
    ):
        await _put(
            session,
            FactKeywordDaily(
                source="google",
                dataset_id=g,
                keyword_id=kw1.id,
                date=day,
                cost=_d(cost),
                impressions=impressions,
                clicks=clicks,
                conversions=None if conversions is None else _d(conversions),
                historical_quality_score=quality,
                average_cpc=None if cpc is None else _d(cpc),
                fetched_at=NOW,
            ),
        )
    exact = ("acme software", "EXACT")
    for term, criterion, day, status, keyword, metrics_row in (
        ("acme pricing", "7001", START, "NONE", exact, ("1", 1, 10, None, None)),
        ("acme pricing", "7001", END, "EXCLUDED", exact, ("2", 2, 20, None, None)),
        ("acme pricing", "7001", BEFORE, "ADDED", ("acme", "BROAD"), ("50", 50, 500, "5", "5")),
        ("acme reviews", "", MID, "NONE", None, ("4", 2, 8, "0.5", "12")),
        (
            "acme alternatives",
            "7002",
            MID,
            "ADDED",
            ("acme tools", "PHRASE"),
            ("1", 1, 3, "1", "40"),
        ),
    ):
        await _search_term(
            session, g, group.id, w.camp_g1, term, criterion, day, status, keyword, metrics_row
        )

    # Snapshots: campaign observations around both ends of the observation span, a budget
    # and a conversion action inside it, and keyword quality scores around Google's window.
    member = {"budget_daily": _d(50), "budget_amount_micros": 50_000_000, "budget_id": w.b_shared}
    payload = {"raw_status": "ENABLED", "explicitly_shared": True, "budget_period": "DAILY"}
    for instant, status in (
        (OBS_LOWER - SECOND, "active"),
        (OBS_LOWER, "active"),
        (MID_NOON, "paused"),
        (OBS_UPPER - SECOND, "active"),
        (OBS_UPPER, "active"),
    ):
        await _snapshot(
            session,
            "google",
            g,
            "campaign",
            w.camp_g1,
            instant,
            status=status,
            payload=payload,
            **member,
        )
    await _snapshot(
        session,
        "google",
        g,
        "budget",
        w.b_shared,
        MID_06,
        budget_daily=_d(50),
        budget_amount_micros=50_000_000,
        payload={"period": "DAILY", "explicitly_shared": True},
    )
    await _snapshot(
        session,
        "google",
        g,
        "conversion_action",
        ca1.id,
        MID_06,
        status="ENABLED",
        payload={"counting_type": "ONE_PER_CLICK", "include_in_conversions_metric": True},
    )
    w.kw1_snapshots = [
        (NY_LOWER - SECOND, 9),
        (NY_LOWER, 8),
        (datetime(2026, 9, 20, tzinfo=UTC), 7),
        (NY_UPPER, 6),
        (NY_UPPER + SECOND, 5),
    ]
    for instant, quality in w.kw1_snapshots:
        await _snapshot(
            session,
            "google",
            g,
            "keyword",
            kw1.id,
            instant,
            status="ENABLED",
            quality_score=quality,
        )
    await _snapshot(
        session,
        "google",
        g,
        "keyword",
        kw2.id,
        datetime(2026, 9, 15, tzinfo=UTC),
        status="ENABLED",
        quality_score=None,
    )

    # The retired Google dataset: the same external ids, dates and internal subject ids.
    old = w.g_old
    old_acct = await _put(
        session,
        AdAccount(
            source="google",
            dataset_id=old,
            external_id="1234567890",
            name="Old",
            currency="EUR",
            timezone="Europe/Berlin",
        ),
    )
    old_budget = await _put(
        session,
        Budget(
            source="google",
            dataset_id=old,
            external_id="9001",
            amount=_d(70),
            amount_micros=70_000_000,
            period="DAILY",
            explicitly_shared=True,
        ),
    )
    old_campaign = await _put(
        session,
        Campaign(
            source="google",
            dataset_id=old,
            account_id=old_acct.id,
            external_id="111",
            name="Old Search Brand",
            status="active",
            raw_status="ENABLED",
            objective_or_channel="SEARCH",
            budget_id=old_budget.id,
            daily_budget=_d(70),
            budget_period="DAILY",
        ),
    )
    w.camp_old = old_campaign.id
    old_group = await _put(
        session,
        AdGroup(
            source="google",
            dataset_id=old,
            campaign_id=old_campaign.id,
            external_id="5001",
            name="Old terms",
            status="ENABLED",
        ),
    )
    old_keyword = await _put(
        session,
        Keyword(
            source="google",
            dataset_id=old,
            ad_group_id=old_group.id,
            criterion_id="7001",
            text="old software",
            match_type="EXACT",
            status="ENABLED",
            quality_score=3,
        ),
    )
    await _put(
        session,
        ConversionAction(
            source="google",
            dataset_id=old,
            external_id="801",
            name="Old demo",
            status="ENABLED",
            include_in_conversions_metric=True,
        ),
    )
    await _google_day(session, old, old_campaign.id, START, "999", 999, 999, "99")
    await _put(
        session,
        FactKeywordDaily(
            source="google",
            dataset_id=old,
            keyword_id=old_keyword.id,
            date=START,
            cost=_d(99),
            impressions=99,
            clicks=99,
            fetched_at=NOW,
        ),
    )
    await _search_term(
        session,
        old,
        old_group.id,
        old_campaign.id,
        "acme pricing",
        "7001",
        START,
        "NONE",
        exact,
        ("99", 99, 99, "9", None),
    )
    await _snapshot(session, "google", old, "campaign", w.camp_g1, MID_NOON, status="ended")
    await _snapshot(session, "google", old, "keyword", w.kw1, MID_NOON, quality_score=1)


async def _seed_linkedin(session: AsyncSession, w: _World) -> None:
    """Seed the active simulated LinkedIn dataset and a retired one."""
    li = w.li
    acct = await _put(
        session,
        AdAccount(
            source="linkedin",
            dataset_id=li,
            external_id="50001",
            name="LinkedIn Account",
            currency="EUR",
            timezone=None,
        ),
    )
    w.acct_l = acct.id
    group = await _put(
        session,
        CampaignGroup(
            source="linkedin",
            dataset_id=li,
            account_id=acct.id,
            external_id="600",
            name="Always on",
            status="ACTIVE",
            daily_budget=_d(100),
            total_budget=None,
        ),
    )
    w.group = group.id
    camp1 = await _put(
        session,
        Campaign(
            source="linkedin",
            dataset_id=li,
            account_id=acct.id,
            external_id="701",
            name="LI Lead Gen",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            campaign_group_id=group.id,
            daily_budget=_d(40),
        ),
    )
    camp2 = await _put(
        session,
        Campaign(
            source="linkedin",
            dataset_id=li,
            account_id=acct.id,
            external_id="702",
            name="LI Website",
            status="paused",
            raw_status="PAUSED",
            objective_or_channel="WEBSITE_CONVERSION",
            campaign_group_id=group.id,
            total_budget=_d(1000),
        ),
    )
    w.camp_l1, w.camp_l2 = camp1.id, camp2.id
    creatives: list[int] = []
    for campaign, urn, status, headline, cta, form in (
        (camp1.id, URN_1, "ACTIVE", "Get the guide", "DOWNLOAD", FORM_A),
        (camp1.id, URN_2, "PAUSED", None, None, FORM_A),
        (camp2.id, URN_3, "ACTIVE", "Visit us", "LEARN_MORE", None),
    ):
        row = await _put(
            session,
            Creative(
                source="linkedin",
                dataset_id=li,
                campaign_id=campaign,
                urn=urn,
                intended_status=status,
                post_urn=None,
                headline=headline,
                intro_text=None if headline is None else f"{headline}.",
                cta_label=cta,
                lead_form_urn=form,
            ),
        )
        creatives.append(row.id)
    w.cr1, w.cr2, w.cr3 = creatives

    await _creative_day(
        session, li, w.cr1, START, cost="10", impressions=1000, clicks=20, form_opens=50, leads=5
    )
    await _creative_day(
        session, li, w.cr2, START, cost="5", impressions=500, clicks=10, form_opens=20
    )
    await _creative_day(session, li, w.cr1, END)
    await _creative_day(
        session, li, w.cr2, MID, cost="0", impressions=0, clicks=0, form_opens=0, leads=0
    )
    await _creative_day(
        session,
        li,
        w.cr3,
        MID,
        cost="8",
        impressions=400,
        clicks=4,
        landing_page_clicks=6,
        website_conversions=2,
    )
    await _creative_day(session, li, w.cr1, BEFORE, cost="100", impressions=100, clicks=100)

    for facet, value, window_start, impressions, clicks, cost, leads in (
        ("job_function", "urn:li:function:1", START, 300, 6, "12", 3),
        ("seniority", "urn:li:seniority:5", START, None, None, None, None),
        ("job_function", "urn:li:function:1", END - timedelta(days=13), 100, 2, "4", 1),
    ):
        await _put(
            session,
            FactSegmentWindow(
                source="linkedin",
                dataset_id=li,
                campaign_id=camp1.id,
                facet=facet,
                value=value,
                window_start=window_start,
                window_end=END,
                impressions=impressions,
                clicks=clicks,
                cost=None if cost is None else _d(cost),
                leads=leads,
                fetched_at=NOW,
            ),
        )
    await _snapshot(
        session,
        "linkedin",
        li,
        "campaign",
        camp1.id,
        MID_06,
        status="active",
        budget_daily=_d(40),
        payload={"raw_status": "ACTIVE"},
    )
    await _snapshot(session, "linkedin", li, "campaign_group", group.id, MID_06, status="ACTIVE")
    await _snapshot(session, "linkedin", li, "creative", w.cr1, MID_06, status="ACTIVE")

    # The retired LinkedIn dataset: a campaign, a creative with facts, a segment and a lead.
    old = w.li_old
    old_acct = await _put(
        session,
        AdAccount(source="linkedin", dataset_id=old, external_id="50001", currency="USD"),
    )
    old_campaign = await _put(
        session,
        Campaign(
            source="linkedin",
            dataset_id=old,
            account_id=old_acct.id,
            external_id="701",
            name="Old LI",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
        ),
    )
    old_creative = await _put(
        session,
        Creative(
            source="linkedin",
            dataset_id=old,
            campaign_id=old_campaign.id,
            urn=URN_1,
            lead_form_urn=FORM_A,
        ),
    )
    await _creative_day(
        session, old, old_creative.id, START, cost="777", impressions=7, clicks=7, leads=7
    )
    for window_start in (START, BASELINE_START):
        await _put(
            session,
            FactSegmentWindow(
                source="linkedin",
                dataset_id=old,
                campaign_id=old_campaign.id,
                facet="industry",
                value="urn:li:industry:4",
                window_start=window_start,
                window_end=END,
                impressions=1,
                clicks=1,
                cost=_d(1),
                leads=1,
                fetched_at=NOW,
            ),
        )
    old_lead = await _lead(session, old, "resp-old", MID_NOON, old_campaign.id, old_creative.id)
    old_contact = await _contact(session, w.c_old, "540", lifecycle_stage="lead")
    await _match(session, old_lead, w.c_old, "matched", contact=old_contact.id, read="ok")


async def _seed_crm(session: AsyncSession, w: _World) -> None:
    """Seed the leads, the companion and primary CRM datasets and a retired HubSpot one."""
    lead_a = await _lead(session, w.li, "resp-a", UTC_LOWER, w.camp_l1, w.cr1)
    lead_b = await _lead(
        session, w.li, "resp-b", datetime(2026, 9, 20, 10, tzinfo=UTC), w.camp_l1, None
    )
    lead_f = await _lead(
        session, w.li, "resp-f", datetime(2026, 9, 25, tzinfo=UTC), w.camp_l1, w.cr2, version=4
    )
    lead_c = await _lead(
        session, w.li, "resp-c", UTC_UPPER - MICRO, w.camp_l2, w.cr3, form=FORM_B, version=1
    )
    await _lead(session, w.li, "resp-d", UTC_UPPER, w.camp_l1, w.cr1)
    await _lead(session, w.li, "resp-e", UTC_LOWER - MICRO, w.camp_l1, w.cr1)
    w.lead_a, w.lead_b, w.lead_c, w.lead_f = lead_a, lead_b, lead_c, lead_f

    # The companion CRM dataset of the simulated LinkedIn dataset.
    c = w.c
    sim1 = await _contact(
        session,
        c,
        "501",
        lifecycle_stage="opportunity",
        mql_at=datetime(2026, 9, 10, tzinfo=UTC),
        ql_at=datetime(2026, 9, 15, tzinfo=UTC),
        google_campaign_ref="111",
        google_touch_at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    sim2 = await _contact(session, c, "502", lifecycle_stage="lead")
    await _contact(session, c, "503", lifecycle_stage="subscriber")
    await _match(session, lead_a, c, "matched", contact=sim1.id, read="ok")
    await _match(session, lead_b, c, "no_email", reason="form map mismatch")
    await _match(session, lead_f, c, "matched", contact=sim2.id, read="error")
    for hs_id in ("601", "603"):
        await _put(
            session,
            CrmMeeting(
                source="hubspot",
                dataset_id=c,
                hs_id=hs_id,
                created_at=datetime(2026, 9, 12, 10, tzinfo=UTC),
                start_at=datetime(2026, 9, 13, 15, tzinfo=UTC),
                meeting_timestamp=datetime(2026, 9, 13, 15, tzinfo=UTC),
                outcome="COMPLETED",
                meeting_source="MEETINGS_PUBLIC",
            ),
        )
    w.deal_instants = {
        "700": UTC_LOWER - MICRO,
        "701": datetime(2026, 9, 1, tzinfo=UTC),
        "702": UTC_LOWER,
        "703": datetime(2026, 9, 30, tzinfo=UTC),
        "704": NOW,
        "705": NOW + MICRO,
        "706": None,
    }
    for hs_id, created_at in w.deal_instants.items():
        await _deal(session, c, hs_id, created_at, "5000" if hs_id == "701" else None)
    for from_type, from_id, to_type, to_id in (
        ("contact", "501", "meeting", "601"),
        ("contact", "501", "meeting", "699"),
        ("contact", "501", "deal", "701"),
        ("contact", "501", "deal", "702"),
        ("contact", "501", "contact", "502"),
        ("contact", "503", "meeting", "603"),
        ("deal", "702", "contact", "501"),
        ("deal", "703", "contact", "599"),
        ("deal", "701", "contact", "501"),
    ):
        await _associate(session, c, from_type, from_id, to_type, to_id)
    for key, status, target, missing in (
        (("contact", "501", "meeting"), "ok", "partial", ("699",)),
        (("contact", "501", "deal"), "ok", "ok", ()),
        (("contact", "502", "meeting"), "ok_empty", None, ()),
        (("contact", "502", "deal"), "error", None, ()),
        (("contact", "503", "meeting"), "ok", "ok", ()),
        (("deal", "702", "contact"), "ok", "ok", ()),
        (("deal", "703", "contact"), "partial", "error", ("599",)),
        (("deal", "704", "contact"), "ok_empty", None, ()),
        (("deal", "701", "contact"), "ok", "ok", ()),
    ):
        await _fetch(session, c, key, status, target, missing)

    # HubSpot's active primary dataset: what the Google-to-CRM link reads.
    h = w.h
    live = await _contact(session, h, "809", lifecycle_stage="customer")
    await _match(session, lead_a, h, "matched", contact=live.id, read="ok")
    await _match(session, lead_c, h, "matched", contact=live.id, read="ok")
    for hs_id, ref, touch in (
        ("820", "111", datetime(2026, 9, 15, tzinfo=UTC)),
        ("821", "112", UTC_UPPER),
        ("822", None, datetime(2026, 9, 20, tzinfo=UTC)),
        ("823", "113", UTC_LOWER),
    ):
        await _contact(
            session,
            h,
            hs_id,
            lifecycle_stage="lead",
            ql_at=datetime(2026, 9, 25, tzinfo=UTC) if hs_id == "820" else None,
            google_campaign_ref=ref,
            google_touch_at=touch,
        )
    await _deal(session, h, "901", datetime(2026, 8, 1, tzinfo=UTC), "1200")
    await _deal(session, h, "902", datetime(2026, 9, 10, tzinfo=UTC))
    await _associate(session, h, "contact", "820", "deal", "901")
    await _associate(session, h, "contact", "809", "meeting", "950")
    await _fetch(session, h, ("contact", "820", "deal"), "ok", "ok")
    await _fetch(session, h, ("contact", "820", "meeting"), "ok_empty", None)
    await _fetch(session, h, ("deal", "902", "contact"), "error", None)
    await _fetch(session, h, ("contact", "809", "meeting"), "ok", "ok")

    # A retired HubSpot dataset with a Google-touch contact and a window deal.
    await _contact(
        session,
        w.h_old,
        "830",
        google_campaign_ref="111",
        google_touch_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    await _deal(session, w.h_old, "940", datetime(2026, 9, 20, tzinfo=UTC))


async def _seed_decisions(session: AsyncSession, w: _World) -> None:
    """Seed decisions and discrepancies, including retired-dataset rows on clashing ids."""
    baseline_at = datetime(2026, 9, 1, tzinfo=UTC)
    tie_at = datetime(2026, 9, 20, tzinfo=UTC)
    member = ("campaign", w.camp_g1)
    await _decision(
        session,
        "google",
        w.g,
        member,
        "shared_member",
        baseline_at,
        status="active",
        budget_id=w.b_shared,
        origin="baseline",
    )
    await _decision(
        session,
        "google",
        w.g,
        member,
        "shared_member",
        tie_at,
        status="paused",
        budget_id=w.b_shared,
    )
    await _decision(
        session,
        "google",
        w.g,
        member,
        "shared_member",
        tie_at,
        status="active",
        budget_id=w.b_shared,
    )
    await _decision(
        session,
        "google",
        w.g,
        ("budget", w.b_shared),
        "budget",
        baseline_at,
        daily="50",
        origin="baseline",
    )
    # A later decision on the same subject id in the retired dataset is never the latest.
    await _decision(
        session,
        "google",
        w.g_old,
        member,
        "own_budget",
        datetime(2026, 10, 1, tzinfo=UTC),
        status="ended",
    )
    await _decision(
        session,
        "linkedin",
        w.li,
        ("campaign", w.camp_l1),
        "own_budget",
        baseline_at,
        status="active",
        daily="40",
        origin="baseline",
    )
    await _decision(
        session,
        "linkedin",
        w.li,
        ("campaign", w.camp_l2),
        "own_budget",
        datetime(2026, 9, 30, tzinfo=UTC),
        status="ended",
        total="1000",
        tombstone=True,
        origin="resolution",
    )

    def at(day: int, hour: int = 0) -> datetime:
        return datetime(2026, 10, day, hour, tzinfo=UTC)

    paused = {"status": "paused"}
    w.d_status = await _discrepancy(
        session,
        "google",
        w.g,
        member,
        "status",
        at(6),
        decided={"status": "active"},
        platform=paused,
    )
    w.d_untracked_own = await _discrepancy(
        session,
        "google",
        w.g,
        ("campaign", w.camp_g3),
        "untracked",
        at(5),
        decided=None,
        platform=paused,
    )
    w.d_untracked_member = await _discrepancy(
        session,
        "google",
        w.g,
        ("campaign", w.camp_g2),
        "untracked",
        at(5, 6),
        decided=None,
        platform={"status": "active"},
        state="awaiting_platform",
    )
    w.d_budget = await _discrepancy(
        session,
        "google",
        w.g,
        ("budget", w.b_shared),
        "budget",
        at(4),
        decided={"daily_budget": "50.000000"},
        platform={"daily_budget": "60.000000"},
    )
    w.d_missing_budget = await _discrepancy(
        session,
        "google",
        w.g,
        ("budget", w.b_orphan),
        "missing",
        at(3),
        decided={"total_budget": None},
        platform=None,
    )
    w.d_unknown_campaign = await _discrepancy(
        session,
        "google",
        w.g,
        ("campaign", 987_654),
        "missing",
        at(2),
        decided={"status": "active"},
        platform=None,
    )
    w.d_unknown_budget = await _discrepancy(
        session,
        "google",
        w.g,
        ("budget", 876_543),
        "missing",
        at(2, 6),
        decided={"daily_budget": "10.000000"},
        platform=None,
    )
    await _discrepancy(
        session,
        "google",
        w.g,
        member,
        "budget",
        at(1),
        decided={"budget_id": w.b_shared},
        platform={"budget_id": w.b_own},
        state="resolved",
    )
    await _discrepancy(
        session,
        "google",
        w.g_old,
        ("campaign", w.camp_old),
        "status",
        at(1),
        decided={"status": "active"},
        platform=paused,
    )
    w.d_linkedin = await _discrepancy(
        session,
        "linkedin",
        w.li,
        ("campaign", w.camp_l1),
        "status",
        at(1, 6),
        decided={"status": "active"},
        platform=paused,
    )


async def _seed_feedback(session: AsyncSession) -> None:
    """Seed feedback: two active rows, one ending exactly at :data:`NOW`, one entity-less."""
    run = await _put(
        session,
        AnalysisRun(
            kind="custom",
            window_start=START,
            window_end=END,
            source_day_boundaries=[],
            trigger="manual",
            data_modes={},
            dataset_ids={},
            config_hash="metrics-load-test",
            status="succeeded",
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=1),
        ),
    )
    item = await _put(
        session,
        Recommendation(
            run_id=run.id,
            section="keywords",
            group="block",
            rule_id="K1",
            entity_type="search_term",
            entity_id="google:search_term:5001|7001|acme pricing",
            rank=1,
            action="Add as negative keyword",
            reason="Spent with no conversions",
            evidence={},
            data_status="live",
        ),
    )
    for rule_id, entity_type, entity_id, kind, until, created in (
        (
            "K1",
            "search_term",
            "google:search_term:5001|7001|acme pricing",
            "dismiss",
            NOW + timedelta(days=30),
            datetime(2026, 10, 1, tzinfo=UTC),
        ),
        (
            "K2",
            "search_term",
            "google:search_term:x",
            "snooze",
            NOW,
            datetime(2026, 9, 29, tzinfo=UTC),
        ),
        ("K3", None, None, "done", NOW + timedelta(days=1), datetime(2026, 9, 28, tzinfo=UTC)),
        (
            "C1",
            "campaign",
            "google:campaign:111",
            "done",
            NOW + SECOND,
            datetime(2026, 9, 30, tzinfo=UTC),
        ),
    ):
        await _put(
            session,
            RecommendationFeedback(
                recommendation_id=item.id,
                rule_id=rule_id,
                entity_type=entity_type,
                entity_id=entity_id,
                kind=kind,
                until=until,
                created_at=created,
                created_by="admin@example.com",
            ),
        )


async def _seed_main(sessions: Sessions) -> _World:
    """Seed the main world in one committed transaction and return its ids."""
    w = _World()
    async with sessions.begin() as session:
        w.g = await _dataset(session, "google", "live", "active")
        w.g_old = await _dataset(session, "google", "live", "retired")
        w.li = await _dataset(session, "linkedin", "simulated", "active")
        w.c = await _dataset(
            session, "hubspot", "simulated", "active", parent=w.li, bindings=UNDECODABLE_BINDINGS
        )
        w.li_old = await _dataset(session, "linkedin", "simulated", "retired")
        w.c_old = await _dataset(session, "hubspot", "simulated", "retired", parent=w.li_old)
        w.h = await _dataset(
            session, "hubspot", "live", "active", bindings=VALID_BINDINGS.to_json()
        )
        w.h_old = await _dataset(session, "hubspot", "live", "retired")
        await _seed_google(session, w)
        await _seed_linkedin(session, w)
        await _seed_crm(session, w)
        await _seed_decisions(session, w)
        await _seed_feedback(session)
        await _freshness(session, "google", w.g, "google.metrics_daily")
        await _freshness(session, "linkedin", w.li, "linkedin.leads")
        await _freshness(session, "hubspot", w.c, "hubspot.deal_search")
        await _freshness(
            session, "hubspot", w.c, "hubspot.contacts", "incomplete", reason="batch read failed"
        )
        await _freshness(
            session,
            "hubspot",
            w.h,
            "hubspot.deal_search",
            "incomplete",
            reason="partition above 10,000",
        )
        await _freshness(session, "google", w.g_old, "google.metrics_daily")
        await _freshness(session, "hubspot", w.h_old, "hubspot.contacts")
        await _freshness(session, "hubspot", w.c_old, "hubspot.deal_search")
    return w


@pytest.fixture
async def world(sessions: Sessions) -> _World:
    """The seeded main world."""
    return await _seed_main(sessions)


@pytest.fixture
async def context(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds, log: _Log
) -> AnalysisContext:
    """The main world's context, loaded in a fresh session as the runner loads it."""
    async with sessions() as session:
        return await load_context(session, window, thresholds, kind="default", now=NOW)


def _scope(
    session: AsyncSession,
    window: AnalysisWindow,
    thresholds: Thresholds,
    datasets: Mapping[Source, DatasetRef],
) -> QueryScope:
    """A scope over ``datasets`` with no CRM dataset, at :data:`NOW`."""
    return QueryScope(
        session=session,
        window=window,
        thresholds=thresholds,
        datasets=datasets,
        linkedin_crm_id=None,
        google_crm_id=None,
        now=NOW,
    )


def _main_refs(w: _World) -> dict[Source, DatasetRef]:
    """The active primary datasets of the main world."""
    return {
        "google": DatasetRef(w.g, "google", "live"),
        "linkedin": DatasetRef(w.li, "linkedin", "simulated"),
        "hubspot": DatasetRef(w.h, "hubspot", "live"),
    }


# ---------------------------------------------------------------------------------------
# Pure: scope lookups, value conversions and argument checks
# ---------------------------------------------------------------------------------------


def test_window_and_bounds_constants_match_the_default_window(window: AnalysisWindow) -> None:
    assert (window.start, window.end, window.days) == (START, END, 28)
    assert window.utc_bounds() == (UTC_LOWER, UTC_UPPER)
    assert metrics._observation_bounds(window, 6) == (OBS_LOWER, OBS_UPPER)
    assert AnalysisWindow.day_bounds(START, GOOGLE_TZ)[0] == NY_LOWER
    assert AnalysisWindow.day_bounds(END, GOOGLE_TZ)[1] == NY_UPPER


def test_observation_bounds_widen_the_margin_for_a_long_gap(window: AnalysisWindow) -> None:
    margin = timedelta(hours=15 + 40)
    assert metrics._observation_bounds(window, 40) == (UTC_LOWER - margin, UTC_UPPER + margin)


@pytest.mark.anyio
async def test_query_scope_dataset_lookups(window: AnalysisWindow, thresholds: Thresholds) -> None:
    session = AsyncSession()
    try:
        full = _scope(
            session,
            window,
            thresholds,
            {
                "google": DatasetRef(3, "google", "live"),
                "linkedin": DatasetRef(5, "linkedin", "simulated"),
            },
        )
        assert (full.dataset_id("google"), full.dataset_id("linkedin")) == (3, 5)
        assert full.dataset_id("hubspot") is None
        assert full.ad_dataset_ids() == [3, 5]
        linkedin_only = _scope(
            session, window, thresholds, {"linkedin": DatasetRef(5, "linkedin", "live")}
        )
        assert linkedin_only.ad_dataset_ids() == [5]
        assert _scope(session, window, thresholds, {}).ad_dataset_ids() == []
    finally:
        await session.close()


def test_value_conversions_keep_unreported_values_none() -> None:
    assert metrics._as_int(None) == 0
    assert metrics._as_int(Decimal(12)) == 12
    assert metrics._as_optional_int(None) is None
    assert metrics._as_optional_int(4) == 4
    assert metrics._as_decimal(None) == Decimal(0)
    assert metrics._as_decimal(3) == Decimal(3)
    assert metrics._as_optional_decimal(None) is None
    assert metrics._as_optional_decimal(Decimal("1.5")) == Decimal("1.5")


@pytest.mark.parametrize("value", [1.5, True, "3"])
def test_value_conversions_reject_non_numeric_aggregates(value: object) -> None:
    with pytest.raises(TypeError, match="expected an integer aggregate"):
        metrics._as_int(value)
    with pytest.raises(TypeError, match="expected a numeric value"):
        metrics._as_decimal(value)


def test_enumeration_choices() -> None:
    assert metrics._choice("google", ("google", "linkedin"), "source") == "google"
    assert metrics._optional_choice(None, ("ok",), "status") is None
    with pytest.raises(ValueError, match=r"status must be one of \('ok',\), found 'bad'"):
        metrics._optional_choice("bad", ("ok",), "status")


@pytest.mark.anyio
async def test_aggregate_rejects_an_unknown_grain(
    window: AnalysisWindow, thresholds: Thresholds
) -> None:
    session = AsyncSession()
    try:
        with pytest.raises(ValueError, match="grain must be one of"):
            await aggregate_sql(_scope(session, window, thresholds, {}), "campaign_week")  # type: ignore[call-overload]
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("now", "kind", "error", "match"),
    [
        (NOW.replace(tzinfo=None), "default", ValueError, "found a naive datetime"),
        (date(2026, 10, 7), "default", TypeError, "now must be a tz-aware datetime"),
        ("2026-10-07T12:00:00Z", "default", TypeError, "now must be a tz-aware datetime"),
        (NOW, "manual", ValueError, "kind must be one of"),
    ],
)
async def test_load_context_rejects_a_bad_now_or_kind(
    now: object,
    kind: str,
    error: type[Exception],
    match: str,
    window: AnalysisWindow,
    thresholds: Thresholds,
) -> None:
    session = AsyncSession()
    try:
        with pytest.raises(error, match=match):
            await load_context(session, window, thresholds, kind=kind, now=now)  # type: ignore[arg-type]
    finally:
        await session.close()


def test_bindings_of_a_dataset_never_verified_or_undecodable(log: _Log) -> None:
    never = SourceDataset(id=4, source="hubspot", mode="live", role="primary", crm_bindings=None)
    assert metrics._bindings(never, NOW) == CrmBindings.unavailable(
        "CRM stage bindings not verified", at=NOW
    )
    broken = SourceDataset(
        id=5, source="hubspot", mode="live", role="primary", crm_bindings=UNDECODABLE_BINDINGS
    )
    assert metrics._bindings(broken, NOW) == CrmBindings.unavailable(
        "CRM stage bindings unreadable", at=NOW
    )
    [(level, fields)] = log.named("analysis_crm_bindings_unreadable")
    assert (level, fields["dataset_id"], fields["key"]) == ("warning", 5, "verified_at")
    valid = SourceDataset(
        id=6, source="hubspot", mode="live", role="primary", crm_bindings=VALID_BINDINGS.to_json()
    )
    assert metrics._bindings(valid, NOW) == VALID_BINDINGS


# ---------------------------------------------------------------------------------------
# aggregate_sql(): every grain on the main world
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_aggregate_campaign_day_google_reads_native_dates_of_the_active_dataset(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        rows = await aggregate_sql(
            _scope(session, window, thresholds, _main_refs(world)), "campaign_day_google"
        )
    assert rows == [
        CampaignDay(
            world.camp_g1,
            DayFact(
                date=START,
                cost=_d("10.5"),
                impressions=100,
                clicks=10,
                conversions=_d("1.5"),
                conversions_value=_d(30),
                budget_lost_is=_d("0.12"),
                impression_share=_d("0.8"),
            ),
        ),
        # Unreported conversions, value and shares stay None: unavailable, never 0.
        CampaignDay(world.camp_g1, DayFact(date=MID, cost=_d(5), impressions=50, clicks=4)),
        CampaignDay(
            world.camp_g1,
            DayFact(
                date=END,
                cost=_d(7),
                impressions=70,
                clicks=7,
                conversions=_d(0),
                conversions_value=_d(0),
            ),
        ),
        CampaignDay(
            world.camp_g3,
            DayFact(date=MID, cost=_d(2), impressions=10, clicks=1, conversions=_d(0)),
        ),
    ]
    assert rows[1].fact.conversions is None
    assert rows[1].fact.budget_lost_is is None


@pytest.mark.anyio
async def test_aggregate_campaign_day_linkedin_sums_creatives_per_utc_date(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        rows = await aggregate_sql(
            _scope(session, window, thresholds, _main_refs(world)), "campaign_day_linkedin"
        )
    assert rows == [
        CampaignDay(
            world.camp_l1,
            DayFact(
                date=START,
                cost=_d(15),
                impressions=1500,
                clicks=30,
                form_opens=70,
                leads=5,
            ),
        ),
        CampaignDay(
            world.camp_l1,
            DayFact(date=MID, cost=_d(0), impressions=0, clicks=0, form_opens=0, leads=0),
        ),
        # Every creative reported nothing: the required counters sum to 0, the rest stay None.
        CampaignDay(world.camp_l1, DayFact(date=END, cost=_d(0), impressions=0, clicks=0)),
        CampaignDay(
            world.camp_l2,
            DayFact(
                date=MID,
                cost=_d(8),
                impressions=400,
                clicks=4,
                landing_page_clicks=6,
                website_conversions=2,
            ),
        ),
    ]
    end_day = rows[2].fact
    assert (end_day.leads, end_day.form_opens, end_day.website_conversions) == (None, None, None)


@pytest.mark.anyio
async def test_aggregate_creative_window_totals_and_delivery_days(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        rows = await aggregate_sql(
            _scope(session, window, thresholds, _main_refs(world)), "creative_window"
        )
    assert rows == [
        CreativeTotals(world.cr1, world.camp_l1, _d(10), 1000, 20, 50, 5, None, None, 1),
        CreativeTotals(world.cr2, world.camp_l1, _d(5), 500, 10, 20, 0, None, None, 1),
        CreativeTotals(world.cr3, world.camp_l2, _d(8), 400, 4, None, None, 6, 2, 1),
    ]


@pytest.mark.anyio
async def test_aggregate_keyword_day(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        rows = await aggregate_sql(
            _scope(session, window, thresholds, _main_refs(world)), "keyword_day"
        )
    assert rows == [
        KeywordDay(world.kw1, START, _d(2), 2, 20, _d(1), 7, _d(1)),
        KeywordDay(world.kw1, END, _d(3), 1, 10, None, None, None),
    ]


@pytest.mark.anyio
async def test_aggregate_search_term_window_takes_status_and_keyword_of_the_latest_date(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        rows = await aggregate_sql(
            _scope(session, window, thresholds, _main_refs(world)), "search_term_window"
        )
    # ORDER BY ad group, criterion present first, criterion, term: the '' criterion (no
    # keyword segment, read back as None) sorts after every criterion id.
    assert rows == [
        SearchTermRow(
            ad_group_id=world.ag,
            campaign_id=world.camp_g1,
            search_term="acme pricing",
            term_status="EXCLUDED",
            matched_criterion_id="7001",
            matched_keyword_text="acme software",
            matched_match_type="EXACT",
            cost=_d(3),
            clicks=3,
            impressions=30,
            conversions=_d(0),
            conversions_value=None,
        ),
        SearchTermRow(
            ad_group_id=world.ag,
            campaign_id=world.camp_g1,
            search_term="acme alternatives",
            term_status="ADDED",
            matched_criterion_id="7002",
            matched_keyword_text="acme tools",
            matched_match_type="PHRASE",
            cost=_d(1),
            clicks=1,
            impressions=3,
            conversions=_d(1),
            conversions_value=_d(40),
        ),
        SearchTermRow(
            ad_group_id=world.ag,
            campaign_id=world.camp_g1,
            search_term="acme reviews",
            term_status="NONE",
            matched_criterion_id=None,
            matched_keyword_text=None,
            matched_match_type=None,
            cost=_d(4),
            clicks=2,
            impressions=8,
            conversions=_d("0.5"),
            conversions_value=_d(12),
        ),
    ]


@pytest.mark.anyio
async def test_aggregate_google_rate_baseline_spans_k1_baseline_days(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    assert thresholds.int("k1.baseline_days") == 90
    async with sessions() as session:
        rows = await aggregate_sql(
            _scope(session, window, thresholds, _main_refs(world)), "google_rate_baseline"
        )
    # START..END (1.5 + unreported + 0, 10 + 4 + 7 clicks), the day before the window
    # (1, 3), the first day of the span (2, 20) and camp_g3 (0, 1); the day before the span,
    # today and the retired dataset are left out. SUM skips the unreported day's conversions
    # but counts its clicks; the Google parser stores 0 for an omitted additive metric, so a
    # synced Google day always carries a number.
    assert rows == [RateBaseline(conversions=_d("4.5"), clicks=45, start=BASELINE_START, end=END)]


@pytest.mark.anyio
async def test_aggregate_segment_window_reads_only_this_exact_window(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        rows = await aggregate_sql(
            _scope(session, window, thresholds, _main_refs(world)), "segment_window"
        )
    assert rows == [
        SegmentRow(
            world.camp_l1, "job_function", "urn:li:function:1", START, END, 300, 6, _d(12), 3
        ),
        SegmentRow(world.camp_l1, "seniority", "urn:li:seniority:5", START, END),
    ]
    assert rows[1].leads is None


@pytest.mark.anyio
async def test_aggregate_returns_nothing_for_a_source_without_an_active_dataset(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        scope = _scope(session, window, thresholds, {})
        for grain in GRAINS:
            assert await aggregate_sql(scope, grain) == [], grain


@pytest.mark.anyio
async def test_aggregate_google_rate_baseline_is_empty_without_any_fact(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions.begin() as session:
        google = await _dataset(session, "google", "live", "active")
    async with sessions() as session:
        refs: dict[Source, DatasetRef] = {"google": DatasetRef(google, "google", "live")}
        scope = _scope(session, window, thresholds, refs)
        assert await aggregate_sql(scope, "google_rate_baseline") == []
        assert await aggregate_sql(scope, "campaign_day_google") == []


async def _sql_and_context_grains(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> dict[str, tuple[list[Any], list[Any]]]:
    """Per grain, ``(aggregate_sql rows, aggregate rows of the loaded context)``."""
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
        scope = _scope(session, window, thresholds, context.datasets)
        return {
            grain: (await aggregate_sql(scope, grain), aggregate(context, grain))
            for grain in GRAINS
        }


@pytest.mark.anyio
async def test_aggregate_reads_every_sql_grain_back_from_the_loaded_context(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    grains = await _sql_and_context_grains(sessions, window, thresholds)
    assert list(grains) == list(GRAINS)
    for grain, (sql_rows, context_rows) in grains.items():
        # Every grain of the main world has rows, so equality pins rows and their order.
        assert sql_rows, grain
        assert context_rows == sql_rows, grain


@pytest.mark.anyio
async def test_aggregate_of_an_empty_database_context_matches_the_empty_sql_grains(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    grains = await _sql_and_context_grains(sessions, window, thresholds)
    assert grains == {grain: ([], []) for grain in GRAINS}


# ---------------------------------------------------------------------------------------
# load_context(): the main world
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_load_context_routes_each_source_to_its_dataset(
    context: AnalysisContext, world: _World
) -> None:
    assert dict(context.datasets) == _main_refs(world)
    assert (context.now, context.run_kind, context.window.start) == (NOW, "default", START)
    linkedin_crm, google_crm = context.linkedin_crm, context.google_crm
    assert linkedin_crm is not None
    assert google_crm is not None
    # A simulated LinkedIn dataset reads its companion; the Google link reads HubSpot's primary.
    assert linkedin_crm.dataset == DatasetRef(world.c, "hubspot", "simulated", "companion")
    assert google_crm.dataset == DatasetRef(world.h, "hubspot", "live", "primary")
    assert linkedin_crm.bindings == CrmBindings.unavailable("CRM stage bindings unreadable", at=NOW)
    assert google_crm.bindings == VALID_BINDINGS
    assert set(context.freshness) == {
        (world.g, "google.metrics_daily"),
        (world.li, "linkedin.leads"),
        (world.c, "hubspot.deal_search"),
        (world.c, "hubspot.contacts"),
        (world.h, "hubspot.deal_search"),
    }
    assert context.freshness[(world.c, "hubspot.contacts")] == Freshness(
        dataset_id=world.c,
        dataset="hubspot.contacts",
        last_attempt_at=NOW - timedelta(hours=1),
        last_success_at=NOW - timedelta(hours=1),
        covered_from=date(2026, 4, 10),
        covered_through=END,
        completeness="incomplete",
        reason="batch read failed",
    )


@pytest.mark.anyio
async def test_load_context_entities_of_the_active_datasets(
    context: AnalysisContext, world: _World
) -> None:
    assert dict(context.accounts) == {
        world.acct_g: AccountInfo(
            world.acct_g, "google", "1234567890", "1234567890", "USD", GOOGLE_TZ
        ),
        world.acct_g2: AccountInfo(
            world.acct_g2, "google", "2223334444", "Canada", "CAD", "America/Toronto"
        ),
        world.acct_l: AccountInfo(world.acct_l, "linkedin", "50001", "LinkedIn Account", "EUR"),
    }
    assert dict(context.campaign_groups) == {
        world.group: CampaignGroupInfo(
            world.group, world.acct_l, "600", "Always on", "ACTIVE", _d(100)
        )
    }

    def google(
        campaign_id: int,
        account: int,
        external: str,
        name: str,
        status: CampaignStatus,
        raw: str,
        budget: int,
    ) -> CampaignInfo:
        daily = {world.b_shared: 50, world.b_own: 20, world.b_cad: 30}[budget]
        return CampaignInfo(
            id=campaign_id,
            source="google",
            account_id=account,
            external_id=external,
            name=name,
            status=status,
            raw_status=raw,
            objective_or_channel="SEARCH",
            objective_group=objective_group("google", "SEARCH"),
            campaign_group_id=None,
            budget_id=budget,
            daily_budget=_d(daily),
            total_budget=None,
            budget_period="DAILY",
            currency="CAD" if account == world.acct_g2 else "USD",
        )

    assert dict(context.campaigns) == {
        world.camp_g1: google(
            world.camp_g1, world.acct_g, "111", "Search Brand", "active", "ENABLED", world.b_shared
        ),
        world.camp_g2: google(
            world.camp_g2,
            world.acct_g,
            "112",
            "Search Generic",
            "active",
            "ENABLED",
            world.b_shared,
        ),
        world.camp_g3: google(
            world.camp_g3, world.acct_g, "113", "Search Own", "paused", "PAUSED", world.b_own
        ),
        world.camp_g4: google(
            world.camp_g4, world.acct_g2, "114", "Canada Search", "active", "ENABLED", world.b_cad
        ),
        world.camp_l1: CampaignInfo(
            id=world.camp_l1,
            source="linkedin",
            account_id=world.acct_l,
            external_id="701",
            name="LI Lead Gen",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            objective_group="linkedin_lead_gen",
            campaign_group_id=world.group,
            budget_id=None,
            daily_budget=_d(40),
            total_budget=None,
            budget_period=None,
            currency="EUR",
        ),
        world.camp_l2: CampaignInfo(
            id=world.camp_l2,
            source="linkedin",
            account_id=world.acct_l,
            external_id="702",
            name="LI Website",
            status="paused",
            raw_status="PAUSED",
            objective_or_channel="WEBSITE_CONVERSION",
            objective_group="linkedin_other",
            campaign_group_id=world.group,
            budget_id=None,
            daily_budget=None,
            total_budget=_d(1000),
            budget_period=None,
            currency="EUR",
        ),
    }
    # A budget takes the currency of a campaign drawing from it, else the first Google
    # account's; the table stores no name, so the display name is the labelled external id.
    assert dict(context.budgets) == {
        world.b_shared: BudgetInfo(
            world.b_shared, "9001", "", _d(50), 50_000_000, "DAILY", True, "USD"
        ),
        world.b_own: BudgetInfo(world.b_own, "9002", "", _d(20), 20_000_000, "DAILY", False, "USD"),
        world.b_orphan: BudgetInfo(
            world.b_orphan, "9003", "", None, None, "CUSTOM_PERIOD", False, "USD"
        ),
        world.b_cad: BudgetInfo(world.b_cad, "9004", "", _d(30), 30_000_000, "DAILY", False, "CAD"),
    }
    assert [budget.name for budget in context.budgets.values()] == [
        "Shared budget 9001",
        "Budget 9002",
        "Budget 9003",
        "Budget 9004",
    ]
    assert dict(context.ad_groups) == {
        world.ag: AdGroupInfo(world.ag, world.camp_g1, "5001", "Brand terms", "ENABLED")
    }
    assert dict(context.keywords) == {
        world.kw1: KeywordInfo(
            world.kw1, world.ag, world.camp_g1, "7001", "acme software", "EXACT", "ENABLED", 7
        ),
        world.kw2: KeywordInfo(
            world.kw2, world.ag, world.camp_g1, "7002", "acme tools", "PHRASE", "ENABLED", None
        ),
    }
    assert dict(context.creatives) == {
        world.cr1: CreativeInfo(
            world.cr1,
            world.camp_l1,
            URN_1,
            "ACTIVE",
            "Get the guide",
            "Get the guide.",
            "DOWNLOAD",
            FORM_A,
        ),
        world.cr2: CreativeInfo(world.cr2, world.camp_l1, URN_2, "PAUSED", lead_form_urn=FORM_A),
        world.cr3: CreativeInfo(
            world.cr3, world.camp_l2, URN_3, "ACTIVE", "Visit us", "Visit us.", "LEARN_MORE"
        ),
    }
    assert dict(context.conversion_actions) == {
        world.ca1: ConversionActionInfo(
            world.ca1,
            "801",
            "Demo request",
            "ENABLED",
            True,
            "ONE_PER_CLICK",
            "GOOGLE_ADS_LAST_CLICK",
        ),
        world.ca2: ConversionActionInfo(world.ca2, "802", "Newsletter", "HIDDEN", False),
    }


@pytest.mark.anyio
async def test_load_context_facts_are_the_aggregated_grains(
    context: AnalysisContext, world: _World
) -> None:
    assert list(context.campaign_days) == [
        world.camp_g1,
        world.camp_g3,
        world.camp_l1,
        world.camp_l2,
    ]
    assert [day.date for day in context.campaign_days[world.camp_g1]] == [START, MID, END]
    assert [day.date for day in context.campaign_days[world.camp_l1]] == [START, MID, END]
    assert context.campaign_days[world.camp_l2][0].website_conversions == 2
    assert set(context.creative_totals) == {world.cr1, world.cr2, world.cr3}
    assert context.creative_totals[world.cr2].leads == 0
    assert context.creative_totals[world.cr3].leads is None
    assert dict(context.keyword_days) == {
        world.kw1: (
            KeywordDay(world.kw1, START, _d(2), 2, 20, _d(1), 7, _d(1)),
            KeywordDay(world.kw1, END, _d(3), 1, 10, None, None, None),
        )
    }
    assert [row.search_term for row in context.search_terms] == [
        "acme pricing",
        "acme alternatives",
        "acme reviews",
    ]
    assert context.k1_baseline == RateBaseline(_d("4.5"), 45, BASELINE_START, END)
    assert [(row.facet, row.leads) for row in context.segments] == [
        ("job_function", 3),
        ("seniority", None),
    ]
    assert context.segment_windows == ((START, END), (END - timedelta(days=13), END))


@pytest.mark.anyio
async def test_load_context_observations_bracket_the_window(
    context: AnalysisContext, world: _World
) -> None:
    member_payload = {"raw_status": "ENABLED", "explicitly_shared": True, "budget_period": "DAILY"}

    def member(at: datetime, status: str) -> Observation:
        return Observation(
            captured_at=at,
            status=status,
            budget_daily=_d(50),
            budget_amount_micros=50_000_000,
            budget_id=world.b_shared,
            payload=member_payload,
        )

    assert dict(context.observations) == {
        ("budget", world.b_shared): (
            Observation(
                captured_at=MID_06,
                budget_daily=_d(50),
                budget_amount_micros=50_000_000,
                payload={"period": "DAILY", "explicitly_shared": True},
            ),
        ),
        # The span is [lower, upper): the instant before it and its upper end are left out.
        ("campaign", world.camp_g1): (
            member(OBS_LOWER, "active"),
            member(MID_NOON, "paused"),
            member(OBS_UPPER - SECOND, "active"),
        ),
        ("campaign", world.camp_l1): (
            Observation(
                captured_at=MID_06,
                status="active",
                budget_daily=_d(40),
                payload={"raw_status": "ACTIVE"},
            ),
        ),
        ("conversion_action", world.ca1): (
            Observation(
                captured_at=MID_06,
                status="ENABLED",
                payload={"counting_type": "ONE_PER_CLICK", "include_in_conversions_metric": True},
            ),
        ),
        ("keyword", world.kw1): (
            Observation(captured_at=NY_LOWER, status="ENABLED", quality_score=8),
            Observation(captured_at=NY_UPPER, status="ENABLED", quality_score=6),
        ),
        ("keyword", world.kw2): (
            Observation(captured_at=datetime(2026, 9, 15, tzinfo=UTC), status="ENABLED"),
        ),
    }
    payload = context.observations[("campaign", world.camp_g1)][0].payload
    with pytest.raises(TypeError):
        payload["raw_status"] = "PAUSED"  # type: ignore[index]


@pytest.mark.anyio
async def test_load_context_latest_observations_are_the_newest_snapshots_at_or_before_now(
    world: _World, sessions: Sessions, thresholds: Thresholds
) -> None:
    budget_payload = {"period": "DAILY", "explicitly_shared": True}
    async with sessions.begin() as session:
        # The budget's newest snapshot is captured exactly at the run time. One a microsecond
        # later is not read, nor the retired dataset's snapshot under the same internal id.
        for dataset_id, instant, amount in (
            (world.g, NOW, 60),
            (world.g, NOW + MICRO, 80),
            (world.g_old, NOW, 90),
        ):
            await _snapshot(
                session,
                "google",
                dataset_id,
                "budget",
                world.b_shared,
                instant,
                budget_daily=_d(amount),
                budget_amount_micros=amount * 1_000_000,
                payload=budget_payload,
            )
    # A custom window that ended months before the run: even the earliest snapshot of the
    # world lies beyond its observation margin, so its observation span holds nothing.
    past = AnalysisWindow.custom(
        date(2026, 6, 1),
        date(2026, 6, 28),
        config=thresholds,
        sources=SourceDayInfo(GOOGLE_TZ),
        now=NOW,
    )
    assert metrics._observation_bounds(past, 6)[1] < OBS_LOWER - SECOND
    async with sessions() as session:
        context = await load_context(session, past, thresholds, kind="custom", now=NOW)
    assert dict(context.observations) == {}
    assert dict(context.latest_observations) == {
        ("budget", world.b_shared): Observation(
            captured_at=NOW,
            budget_daily=_d(60),
            budget_amount_micros=60_000_000,
            payload=budget_payload,
        ),
        # Snapshots after the run time (OBS_UPPER - 1 s, OBS_UPPER) are left out, and so is
        # the retired dataset's "ended" snapshot at MID_NOON, although it was written later
        # under the same internal id and would win the id tie-break.
        ("campaign", world.camp_g1): Observation(
            captured_at=MID_NOON,
            status="paused",
            budget_daily=_d(50),
            budget_amount_micros=50_000_000,
            budget_id=world.b_shared,
            payload={"raw_status": "ENABLED", "explicitly_shared": True, "budget_period": "DAILY"},
        ),
        ("campaign", world.camp_l1): Observation(
            captured_at=MID_06,
            status="active",
            budget_daily=_d(40),
            payload={"raw_status": "ACTIVE"},
        ),
    }
    assert list(context.latest_observations) == sorted(context.latest_observations)
    payload = context.latest_observations[("campaign", world.camp_g1)].payload
    with pytest.raises(TypeError):
        payload["raw_status"] = "PAUSED"  # type: ignore[index]


@pytest.mark.anyio
async def test_load_context_leads_read_their_match_in_the_companion_only(
    context: AnalysisContext, world: _World
) -> None:
    assert context.leads == (
        LeadInfo(
            id=world.lead_a,
            external_id="resp-a",
            campaign_id=world.camp_l1,
            creative_id=world.cr1,
            creative_urn=URN_1,
            lead_form_urn=FORM_A,
            lead_form_version=3,
            submitted_at=UTC_LOWER,
            match_status="matched",
            contact_hs_id="501",
            contact_read_status="ok",
        ),
        LeadInfo(
            id=world.lead_b,
            external_id="resp-b",
            campaign_id=world.camp_l1,
            creative_id=None,
            creative_urn=None,
            lead_form_urn=FORM_A,
            lead_form_version=3,
            submitted_at=datetime(2026, 9, 20, 10, tzinfo=UTC),
            match_status="no_email",
            match_reason="form map mismatch",
        ),
        LeadInfo(
            id=world.lead_f,
            external_id="resp-f",
            campaign_id=world.camp_l1,
            creative_id=world.cr2,
            creative_urn=URN_2,
            lead_form_urn=FORM_A,
            lead_form_version=4,
            submitted_at=datetime(2026, 9, 25, tzinfo=UTC),
            match_status="matched",
            contact_hs_id="502",
            contact_read_status="error",
        ),
        # Matched only in HubSpot's live primary: no row in the companion, so unresolved.
        LeadInfo(
            id=world.lead_c,
            external_id="resp-c",
            campaign_id=world.camp_l2,
            creative_id=world.cr3,
            creative_urn=URN_3,
            lead_form_urn=FORM_B,
            lead_form_version=1,
            submitted_at=UTC_UPPER - MICRO,
        ),
    )
    assert context.leads[-1].unresolved
    assert "809" not in {lead.contact_hs_id for lead in context.leads}


@pytest.mark.anyio
async def test_load_context_companion_crm_evidence(context: AnalysisContext) -> None:
    crm = context.linkedin_crm
    assert crm is not None
    assert dict(crm.contacts) == {
        "501": ContactInfo(
            "501",
            "opportunity",
            datetime(2026, 9, 10, tzinfo=UTC),
            datetime(2026, 9, 15, tzinfo=UTC),
            "111",
            datetime(2026, 9, 12, tzinfo=UTC),
        ),
        "502": ContactInfo("502", "lead"),
    }
    # Only the matched leads' contacts are read here; Google discovery reads the primary.
    assert crm.google_contacts == ()
    assert dict(crm.meetings) == {
        "601": MeetingInfo(
            "601",
            datetime(2026, 9, 12, 10, tzinfo=UTC),
            datetime(2026, 9, 13, 15, tzinfo=UTC),
            datetime(2026, 9, 13, 15, tzinfo=UTC),
            "COMPLETED",
            "MEETINGS_PUBLIC",
        )
    }

    def deal(hs_id: str, amount: str | None = None) -> DealInfo:
        created = {
            "701": datetime(2026, 9, 1, tzinfo=UTC),
            "702": UTC_LOWER,
            "703": datetime(2026, 9, 30, tzinfo=UTC),
            "704": NOW,
        }[hs_id]
        return DealInfo(
            hs_id,
            created,
            "appointmentscheduled",
            "default",
            None if amount is None else _d(amount),
        )

    # 701 predates the window but is a matched contact's deal; 700, 705 and 706 are neither.
    assert dict(crm.deals) == {
        "701": deal("701", "5000"),
        "702": deal("702"),
        "703": deal("703"),
        "704": deal("704"),
    }
    assert crm.window_deal_ids == ("702", "703", "704")
    assert dict(crm.associations) == {
        ("contact", "501", "deal"): ("701", "702"),
        ("contact", "501", "meeting"): ("601", "699"),
        ("deal", "702", "contact"): ("501",),
        ("deal", "703", "contact"): ("599",),
    }
    assert dict(crm.fetches) == {
        ("contact", "501", "meeting"): AssociationFetchInfo(
            "contact", "501", "meeting", "ok", "partial", ("699",)
        ),
        ("contact", "501", "deal"): AssociationFetchInfo("contact", "501", "deal", "ok", "ok"),
        ("contact", "502", "meeting"): AssociationFetchInfo(
            "contact", "502", "meeting", "ok_empty"
        ),
        ("contact", "502", "deal"): AssociationFetchInfo("contact", "502", "deal", "error"),
        ("deal", "702", "contact"): AssociationFetchInfo("deal", "702", "contact", "ok", "ok"),
        ("deal", "703", "contact"): AssociationFetchInfo(
            "deal", "703", "contact", "partial", "error", ("599",)
        ),
        ("deal", "704", "contact"): AssociationFetchInfo("deal", "704", "contact", "ok_empty"),
    }
    # The associated meeting 699 was never read: absent from meetings, unknown evidence.
    assert "699" not in crm.meetings
    assert not crm.association_known("contact", "501", "meeting")
    assert crm.deal_search_complete


@pytest.mark.anyio
async def test_load_context_google_crm_reads_hubspot_primary(context: AnalysisContext) -> None:
    crm = context.google_crm
    assert crm is not None
    discovered_820 = ContactInfo(
        "820",
        "lead",
        None,
        datetime(2026, 9, 25, tzinfo=UTC),
        "111",
        datetime(2026, 9, 15, tzinfo=UTC),
    )
    discovered_821 = ContactInfo("821", "lead", None, None, "112", UTC_UPPER)
    # Touch within the Google-native [start 00:00, end + 1 day 00:00) New York with a campaign
    # reference: 821 at UTC midnight after the end is still the end date in New York, while
    # 823 at UTC midnight of the start is the evening before it there. The lead-matched live
    # contact 809 and the retired dataset's contact 830 are not read.
    assert crm.google_contacts == (discovered_820, discovered_821)
    assert dict(crm.contacts) == {"820": discovered_820, "821": discovered_821}
    assert dict(crm.associations) == {("contact", "820", "deal"): ("901",)}
    assert set(crm.fetches) == {
        ("contact", "820", "deal"),
        ("contact", "820", "meeting"),
        ("deal", "902", "contact"),
    }
    assert set(crm.deals) == {"901", "902"}
    assert crm.window_deal_ids == ("902",)
    assert dict(crm.meetings) == {}
    # The primary's deal search is incomplete, so the orphan-deal share is unknown.
    assert not crm.deal_search_complete


@pytest.mark.anyio
async def test_load_context_latest_decisions_and_open_discrepancies(
    context: AnalysisContext, world: _World
) -> None:
    baseline_at = datetime(2026, 9, 1, tzinfo=UTC)
    assert dict(context.decisions) == {
        # Two rows at one instant: the later id is the last decided state; the retired
        # dataset's later row on the same subject id is never read.
        ("campaign", world.camp_g1): DecisionInfo(
            "campaign",
            world.camp_g1,
            "shared_member",
            "active",
            None,
            None,
            world.b_shared,
            False,
            datetime(2026, 9, 20, tzinfo=UTC),
            "user",
        ),
        ("budget", world.b_shared): DecisionInfo(
            "budget",
            world.b_shared,
            "budget",
            None,
            _d(50),
            None,
            None,
            False,
            baseline_at,
            "baseline",
        ),
        ("campaign", world.camp_l1): DecisionInfo(
            "campaign",
            world.camp_l1,
            "own_budget",
            "active",
            _d(40),
            None,
            None,
            False,
            baseline_at,
            "baseline",
        ),
        ("campaign", world.camp_l2): DecisionInfo(
            "campaign",
            world.camp_l2,
            "own_budget",
            "ended",
            None,
            _d(1000),
            None,
            True,
            datetime(2026, 9, 30, tzinfo=UTC),
            "resolution",
        ),
    }
    found = [
        (item.id, item.source, item.subject_name, item.shape, item.kind, item.state)
        for item in context.discrepancies
    ]
    assert found == [
        (world.d_linkedin, "linkedin", "LI Lead Gen", "own_budget", "status", "open"),
        (world.d_unknown_campaign, "google", "Campaign #987654", "own_budget", "missing", "open"),
        (world.d_unknown_budget, "google", "Budget #876543", "budget", "missing", "open"),
        (world.d_missing_budget, "google", "Budget 9003", "budget", "missing", "open"),
        (world.d_budget, "google", "Shared budget 9001", "budget", "budget", "open"),
        (world.d_untracked_own, "google", "Search Own", "own_budget", "untracked", "open"),
        (
            world.d_untracked_member,
            "google",
            "Search Generic",
            "shared_member",
            "untracked",
            "awaiting_platform",
        ),
        (world.d_status, "google", "Search Brand", "shared_member", "status", "open"),
    ]
    first_status = context.discrepancies[-1]
    assert first_status == DiscrepancyInfo(
        id=world.d_status,
        subject_type="campaign",
        subject_id=world.camp_g1,
        subject_name="Search Brand",
        source="google",
        shape="shared_member",
        kind="status",
        decided_value={"status": "active"},
        platform_value={"status": "paused"},
        detected_at=datetime(2026, 10, 6, tzinfo=UTC),
        state="open",
    )
    untracked = context.discrepancies[5]
    assert (untracked.decided_value, dict(untracked.platform_value or {})) == (
        None,
        {"status": "paused"},
    )
    assert context.discrepancies[3].platform_value is None


@pytest.mark.anyio
async def test_load_context_active_feedback(context: AnalysisContext) -> None:
    assert context.feedback == (
        FeedbackInfo("C1", "campaign", "google:campaign:111", "done", NOW + SECOND),
        FeedbackInfo(
            "K1",
            "search_term",
            "google:search_term:5001|7001|acme pricing",
            "dismiss",
            NOW + timedelta(days=30),
        ),
    )


@pytest.mark.anyio
async def test_load_context_logs_counts_without_personal_data(
    context: AnalysisContext, world: _World, log: _Log
) -> None:
    [(level, fields)] = log.named("analysis_context_loaded")
    assert level == "info"
    assert fields["run_kind"] == "default"
    assert (fields["window_start"], fields["window_end"], fields["days"]) == (
        "2026-09-09",
        "2026-10-06",
        28,
    )
    assert fields["dataset_ids"] == {"google": world.g, "linkedin": world.li, "hubspot": world.h}
    assert (fields["linkedin_crm_dataset_id"], fields["google_crm_dataset_id"]) == (
        world.c,
        world.h,
    )
    assert fields["counts"] == {
        "freshness": 5,
        "accounts": 3,
        "campaigns": 6,
        "budgets": 4,
        "budgets_without_currency": 0,
        "ad_groups": 1,
        "keywords": 2,
        "creatives": 3,
        "conversion_actions": 2,
        "campaign_days": 8,
        "creative_totals": 3,
        "keyword_days": 2,
        "search_terms": 3,
        "segments": 2,
        "observations": 9,
        "latest_observations": 3,
        "leads": 4,
        "linkedin_crm_contacts": 2,
        "google_crm_contacts": 2,
        "discrepancies": 8,
        "decisions": 4,
        "feedback": 2,
        "feedback_without_entity": 1,
        "segment_windows": 2,
    }
    assert isinstance(fields["elapsed_ms"], int)
    [(warning, bindings)] = log.named("analysis_crm_bindings_unreadable")
    assert (warning, bindings["dataset_id"], bindings["key"]) == ("warning", world.c, "verified_at")
    rendered = repr(log.events)
    assert "@" not in rendered
    assert _email_hash("resp-a") not in rendered


@pytest.mark.anyio
async def test_load_context_writes_nothing_and_is_repeatable(
    world: _World, sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="custom", now=NOW)
        assert context.run_kind == "custom"
        assert not session.new
        assert not session.dirty
        assert session.in_transaction()
    async with sessions() as session:
        again = await load_context(session, window, thresholds, kind="custom", now=NOW)
    assert again == context


# ---------------------------------------------------------------------------------------
# load_context(): other dataset configurations
# ---------------------------------------------------------------------------------------


async def _linkedin_world(session: AsyncSession, mode: str) -> tuple[int, int, int]:
    """Seed an active LinkedIn dataset with one campaign and creative; return their ids."""
    dataset = await _dataset(session, "linkedin", mode, "active")
    account = await _put(
        session,
        AdAccount(source="linkedin", dataset_id=dataset, external_id="51", currency="USD"),
    )
    campaign = await _put(
        session,
        Campaign(
            source="linkedin",
            dataset_id=dataset,
            account_id=account.id,
            external_id="751",
            name="Live LI",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
        ),
    )
    creative = await _put(
        session,
        Creative(
            source="linkedin",
            dataset_id=dataset,
            campaign_id=campaign.id,
            urn=URN_1,
            lead_form_urn=FORM_A,
        ),
    )
    return dataset, campaign.id, creative.id


async def _shared_crm_world(sessions: Sessions, bindings: dict[str, Any] | None) -> int:
    """Seed live LinkedIn and HubSpot datasets that share one CRM load; return HubSpot's id.

    Contact 301 is the matched lead's contact, with one read meeting. Contact 302 stores a
    Google campaign reference and a touch inside the window, with one read deal created
    before the window, so only Google discovery brings 302 and that deal into the load.
    """
    async with sessions.begin() as session:
        linkedin, campaign, creative = await _linkedin_world(session, "live")
        hubspot = await _dataset(session, "hubspot", "live", "active", bindings=bindings)
        lead = await _lead(session, linkedin, "resp-live", MID_NOON, campaign, creative)
        matched = await _contact(session, hubspot, "301", lifecycle_stage="lead")
        await _match(session, lead, hubspot, "matched", contact=matched.id, read="ok")
        await _contact(session, hubspot, "302", google_campaign_ref="999", google_touch_at=MID_NOON)
        await _associate(session, hubspot, "contact", "301", "meeting", "401")
        await _fetch(session, hubspot, ("contact", "301", "meeting"), "ok", "ok")
        await _put(session, CrmMeeting(source="hubspot", dataset_id=hubspot, hs_id="401"))
        await _associate(session, hubspot, "contact", "302", "deal", "402")
        await _fetch(session, hubspot, ("contact", "302", "deal"), "ok", "ok")
        await _deal(session, hubspot, "402", datetime(2026, 8, 1, tzinfo=UTC))
    return hubspot


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param(
            None,
            CrmBindings.unavailable("CRM stage bindings not verified", at=NOW),
            id="never-verified",
        ),
        pytest.param(
            UNDECODABLE_BINDINGS,
            CrmBindings.unavailable("CRM stage bindings unreadable", at=NOW),
            id="undecodable",
        ),
        pytest.param(
            GOOGLE_LINK_UNAVAILABLE.to_json(), GOOGLE_LINK_UNAVAILABLE, id="google-link-unavailable"
        ),
    ],
)
async def test_live_linkedin_and_google_share_one_crm_load(
    sessions: Sessions,
    window: AnalysisWindow,
    thresholds: Thresholds,
    log: _Log,
    stored: dict[str, Any] | None,
    expected: CrmBindings,
) -> None:
    hubspot = await _shared_crm_world(sessions, stored)
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    assert set(context.datasets) == {"linkedin", "hubspot"}
    assert context.linkedin_crm is context.google_crm
    crm = context.linkedin_crm
    assert crm is not None
    assert crm.dataset == DatasetRef(hubspot, "hubspot", "live", "primary")
    assert crm.bindings == expected
    # Without a verified Google link the stored reference of 302 is never discovered, so
    # neither 302 nor its deal 402 enters the load, as in scenarios.context_from_world.
    assert set(crm.contacts) == {"301"}
    assert crm.google_contacts == ()
    assert dict(crm.meetings) == {"401": MeetingInfo("401")}
    assert dict(crm.associations) == {("contact", "301", "meeting"): ("401",)}
    assert set(crm.fetches) == {("contact", "301", "meeting")}
    assert dict(crm.deals) == {}
    assert crm.window_deal_ids == ()
    assert not crm.deal_search_complete
    assert [(lead.match_status, lead.contact_hs_id) for lead in context.leads] == [
        ("matched", "301")
    ]
    # No Google dataset: no Google grain, no keyword, no K1 baseline (unavailable, not 0).
    assert context.k1_baseline is None
    assert context.search_terms == ()
    assert dict(context.keywords) == {}
    assert context.budgets == {}
    [(_, fields)] = log.named("analysis_context_loaded")
    assert (fields["linkedin_crm_dataset_id"], fields["google_crm_dataset_id"]) == (
        hubspot,
        hubspot,
    )
    assert fields["counts"]["google_crm_contacts"] == 0
    # The shared load decodes the stored bindings once: one warning when they are unreadable.
    unreadable = log.named("analysis_crm_bindings_unreadable")
    assert len(unreadable) == (1 if stored is UNDECODABLE_BINDINGS else 0)


@pytest.mark.anyio
async def test_a_verified_google_link_discovers_the_same_rows_in_the_shared_crm_load(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds, log: _Log
) -> None:
    hubspot = await _shared_crm_world(sessions, VALID_BINDINGS.to_json())
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    assert context.linkedin_crm is context.google_crm
    crm = context.google_crm
    assert crm is not None
    assert crm.dataset == DatasetRef(hubspot, "hubspot", "live", "primary")
    assert crm.bindings == VALID_BINDINGS
    # The verified link discovers 302 by its stored reference and touch, with its deal.
    assert set(crm.contacts) == {"301", "302"}
    assert crm.google_contacts == (ContactInfo("302", None, None, None, "999", MID_NOON),)
    assert dict(crm.meetings) == {"401": MeetingInfo("401")}
    assert dict(crm.associations) == {
        ("contact", "301", "meeting"): ("401",),
        ("contact", "302", "deal"): ("402",),
    }
    assert crm.fetch("contact", "302", "deal") == AssociationFetchInfo(
        "contact", "302", "deal", "ok", "ok"
    )
    assert dict(crm.deals) == {
        "402": DealInfo(
            "402", datetime(2026, 8, 1, tzinfo=UTC), "appointmentscheduled", "default", None
        )
    }
    # 402 predates the window: a discovered contact's deal, never a window deal.
    assert crm.window_deal_ids == ()
    [(_, fields)] = log.named("analysis_context_loaded")
    assert fields["counts"]["google_crm_contacts"] == 1


@pytest.mark.anyio
async def test_simulated_linkedin_without_companion_reads_no_match(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions.begin() as session:
        linkedin, campaign, creative = await _linkedin_world(session, "simulated")
        retired = await _dataset(session, "hubspot", "live", "retired")
        lead = await _lead(session, linkedin, "resp-sim", MID_NOON, campaign, creative)
        contact = await _contact(session, retired, "309")
        await _match(session, lead, retired, "matched", contact=contact.id, read="ok")
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    assert set(context.datasets) == {"linkedin"}
    assert context.linkedin_crm is None
    assert context.google_crm is None
    [only] = context.leads
    assert (only.match_status, only.contact_hs_id, only.contact_read_status) == (None, None, None)
    assert only.unresolved


@pytest.mark.anyio
async def test_hubspot_alone_loads_only_the_google_crm_link(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions.begin() as session:
        hubspot = await _dataset(
            session, "hubspot", "live", "active", bindings=VALID_BINDINGS.to_json()
        )
        await _contact(session, hubspot, "320", google_campaign_ref="111", google_touch_at=MID_NOON)
        await _deal(session, hubspot, "921", MID_NOON)
        await _fetch(session, hubspot, ("deal", "921", "contact"), "ok_empty", None)
        await _freshness(session, "hubspot", hubspot, "hubspot.deal_search", covered_from=MID)
        retired = await _dataset(session, "google", "live", "retired")
        await _discrepancy(
            session,
            "google",
            retired,
            ("campaign", 1),
            "status",
            NOW - timedelta(hours=3),
            decided={"status": "active"},
            platform={"status": "paused"},
        )
        await _decision(
            session, "google", retired, ("campaign", 1), "own_budget", MID_NOON, status="active"
        )
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    assert dict(context.datasets) == {"hubspot": DatasetRef(hubspot, "hubspot", "live")}
    assert context.linkedin_crm is None
    crm = context.google_crm
    assert crm is not None
    assert crm.bindings == VALID_BINDINGS
    assert [contact.hs_id for contact in crm.google_contacts] == ["320"]
    assert crm.window_deal_ids == ("921",)
    assert crm.fetch("deal", "921", "contact") == AssociationFetchInfo(
        "deal", "921", "contact", "ok_empty"
    )
    # The search covers only from MID, after the window start: not complete for the window.
    assert not crm.deal_search_complete
    assert set(context.freshness) == {(hubspot, "hubspot.deal_search")}
    for empty in (
        context.accounts,
        context.campaigns,
        context.budgets,
        context.campaign_days,
        context.creative_totals,
        context.keyword_days,
        context.observations,
        context.decisions,
    ):
        assert dict(empty) == {}
    assert (context.leads, context.discrepancies, context.segments) == ((), (), ())
    assert (context.segment_windows, context.k1_baseline) == ((), None)


TOKYO = "Asia/Tokyo"
TOKYO_LOWER = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
"""The default window's first day, 2026-09-09, starts nine hours before UTC in Tokyo."""


@pytest.mark.anyio
@pytest.mark.parametrize(
    "zone_from", ["window-boundary", "first-google-account-with-a-zone"], ids=str
)
async def test_google_touch_discovery_covers_the_native_days_of_a_zone_east_of_utc(
    sessions: Sessions, thresholds: Thresholds, zone_from: str
) -> None:
    # The zone is the window's Google boundary or, without one, the first Google account's
    # zone: the zone native_day_zone returns and shared-budget histories date member days in.
    window = AnalysisWindow.default(
        thresholds,
        SourceDayInfo(TOKYO)
        if zone_from == "window-boundary"
        else SourceDayInfo(None, ("linkedin",)),
        now=NOW,
    )
    assert (window.start, window.end) == (START, END)
    assert AnalysisWindow.day_bounds(START, TOKYO)[0] == TOKYO_LOWER < UTC_LOWER
    async with sessions.begin() as session:
        hubspot = await _dataset(
            session, "hubspot", "live", "active", bindings=VALID_BINDINGS.to_json()
        )
        if zone_from != "window-boundary":
            google = await _dataset(session, "google", "live", "active")
            for external_id, timezone in (("5550000001", None), ("5550000002", TOKYO)):
                await _put(
                    session,
                    AdAccount(
                        source="google",
                        dataset_id=google,
                        external_id=external_id,
                        name=None,
                        currency="USD",
                        timezone=timezone,
                    ),
                )
        for hs_id, touch in (
            ("t-before-both", TOKYO_LOWER - MICRO),
            ("t-on-native-start", TOKYO_LOWER),
            ("t-first-native-day", TOKYO_LOWER + timedelta(hours=1)),
            # After the last Tokyo day (it ends at 15:00 UTC) but inside the UTC window.
            ("t-last-utc-day", UTC_UPPER - timedelta(hours=4)),
            ("t-at-utc-upper", UTC_UPPER),
        ):
            await _contact(
                session, hubspot, hs_id, google_campaign_ref="111", google_touch_at=touch
            )
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    assert metrics.native_day_zone(context, "google") == TOKYO
    crm = context.google_crm
    assert crm is not None
    # Discovery covers the native window of each veto that judges these contacts. With a
    # Tokyo window boundary both vetoes read the Tokyo days, so the last UTC day, after the
    # last Tokyo day, is not read. Without one, the per-campaign veto falls back to the first
    # Google account's zone (none here, so UTC) and the shared-budget veto to the first
    # account with a zone (Tokyo): the union reads the first Tokyo day and the last UTC day.
    # An instant outside every bound is never read.
    expected = ["t-on-native-start", "t-first-native-day"]
    if zone_from != "window-boundary":
        expected.append("t-last-utc-day")
    assert [contact.hs_id for contact in crm.google_contacts] == expected
    assert set(crm.contacts) == set(expected)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sources",
    [
        pytest.param(SourceDayInfo(GOOGLE_TZ), id="window-google-boundary"),
        # Without Google days in the window, the Google account's time zone decides.
        pytest.param(SourceDayInfo(None, ("linkedin",)), id="google-account-time-zone"),
    ],
)
async def test_google_touch_contacts_are_selected_in_the_google_native_day(
    sessions: Sessions, thresholds: Thresholds, sources: SourceDayInfo
) -> None:
    # AAP 0.5.4.1: the window is applied to Google's own days, [start 00:00, end + 1 day
    # 00:00) in New York, never to UTC days (A1-F08).
    touches = {
        "e-ny-2300": NY_UPPER - timedelta(hours=1),  # 23:00 New York on the end date
        "e-ny-midnight": NY_UPPER,  # the day after the end in New York
        "s-ny-midnight": NY_LOWER,  # 00:00 New York on the start date
        "s-ny-2300": NY_LOWER - timedelta(hours=1),  # 23:00 New York the day before the start
    }
    async with sessions.begin() as session:
        google = await _dataset(session, "google", "live", "active")
        await _put(
            session,
            AdAccount(
                source="google",
                dataset_id=google,
                external_id="1234567890",
                name=None,
                currency="USD",
                timezone=GOOGLE_TZ,
            ),
        )
        hubspot = await _dataset(
            session, "hubspot", "live", "active", bindings=VALID_BINDINGS.to_json()
        )
        for hs_id, touch in touches.items():
            await _contact(
                session, hubspot, hs_id, google_campaign_ref="111", google_touch_at=touch
            )
    window = AnalysisWindow.default(thresholds, sources, now=NOW)
    assert (window.start, window.end) == (START, END)
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    crm = context.google_crm
    assert crm is not None
    # Both UTC instants differ from the native ones: e-ny-2300 is after UTC midnight following
    # the end date (UTC bounds would drop it) and s-ny-2300 is after UTC midnight of the start
    # date (UTC bounds would keep it).
    assert touches["e-ny-2300"] >= UTC_UPPER
    assert touches["s-ny-2300"] >= UTC_LOWER
    assert [(contact.hs_id, contact.google_touch_at) for contact in crm.google_contacts] == [
        ("s-ny-midnight", NY_LOWER),
        ("e-ny-2300", NY_UPPER - timedelta(hours=1)),
    ]
    assert set(crm.contacts) == {"e-ny-2300", "s-ny-midnight"}


@pytest.mark.anyio
async def test_an_empty_database_loads_an_empty_context(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds, log: _Log
) -> None:
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    assert dict(context.datasets) == {}
    assert (context.linkedin_crm, context.google_crm, context.k1_baseline) == (None, None, None)
    assert dict(context.freshness) == {}
    assert dict(context.campaigns) == {}
    assert (context.leads, context.feedback, context.discrepancies) == ((), (), ())
    [(_, fields)] = log.named("analysis_context_loaded")
    assert set(fields["counts"].values()) == {0}


@pytest.mark.anyio
async def test_a_google_budget_without_any_currency_is_not_loaded(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds, log: _Log
) -> None:
    async with sessions.begin() as session:
        google = await _dataset(session, "google", "live", "active")
        await _put(
            session,
            Budget(
                source="google",
                dataset_id=google,
                external_id="9100",
                amount=_d(10),
                amount_micros=10_000_000,
                period="DAILY",
                explicitly_shared=True,
            ),
        )
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    assert dict(context.budgets) == {}
    assert context.k1_baseline is None
    [(_, fields)] = log.named("analysis_context_loaded")
    assert (fields["counts"]["budgets"], fields["counts"]["budgets_without_currency"]) == (0, 1)


@pytest.mark.anyio
async def test_a_stored_value_outside_its_enumeration_fails_the_load(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    async with sessions.begin() as session:
        google = await _dataset(session, "google", "live", "active")
        await _put(
            session,
            AdAccount(source="hubspot", dataset_id=google, external_id="1", currency="USD"),
        )
    async with sessions() as session:
        with pytest.raises(ValueError, match=r"ad_account\.source must be one of"):
            await load_context(session, window, thresholds, kind="default", now=NOW)


# ---------------------------------------------------------------------------------------
# The conventions simulation.scenarios.context_from_world documents for load_context
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_window_deals_are_created_from_window_start_to_run_time(
    context: AnalysisContext, world: _World
) -> None:
    # context_from_world: ``lower <= deal.created_at <= now`` with lower = utc_bounds()[0].
    lower = context.window.utc_bounds()[0]
    expected = tuple(
        hs_id
        for hs_id, created in sorted(
            world.deal_instants.items(), key=lambda item: (item[1] or lower, item[0])
        )
        if created is not None and lower <= created <= NOW
    )
    crm = context.linkedin_crm
    assert crm is not None
    assert crm.window_deal_ids == expected == ("702", "703", "704")


@pytest.mark.anyio
async def test_keyword_snapshots_keep_the_first_and_last_inside_google_window(
    context: AnalysisContext, world: _World
) -> None:
    # context_from_world.read_observations: inside ``[day_bounds(start)[0],
    # day_bounds(end)[1]]`` in Google's zone, both ends inclusive, keep the first and last.
    lower, _ = AnalysisWindow.day_bounds(context.window.start, GOOGLE_TZ)
    _, upper = AnalysisWindow.day_bounds(context.window.end, GOOGLE_TZ)
    inside = [snap for snap in world.kw1_snapshots if lower <= snap[0] <= upper]
    expected = [inside[0]] if len(inside) == 1 else [inside[0], inside[-1]]
    read = context.observations[("keyword", world.kw1)]
    assert [(item.captured_at, item.quality_score) for item in read] == expected
    assert expected == [(NY_LOWER, 8), (NY_UPPER, 6)]
    assert len(context.observations[("keyword", world.kw2)]) == 1


@pytest.mark.anyio
async def test_keyword_observations_break_captured_at_ties_by_snapshot_id(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    """Each keyword's per-keyword probes keep the lowest snapshot id at the earliest instant
    inside Google's window and the highest id at the latest; a single observation, or two at
    one instant (the last kept), yields one row; a keyword observed only outside the window,
    or never, is absent; another dataset's or entity type's rows are never read."""
    async with sessions.begin() as session:
        g = await _dataset(session, "google", "live", "active")
        old = await _dataset(session, "google", "live", "retired")
        account = await _put(
            session,
            AdAccount(
                source="google",
                dataset_id=g,
                external_id="1234567890",
                name=None,
                currency="USD",
                timezone=GOOGLE_TZ,
            ),
        )
        campaign = await _put(
            session,
            Campaign(
                source="google",
                dataset_id=g,
                account_id=account.id,
                external_id="111",
                name="Search Brand",
                status="active",
                raw_status="ENABLED",
                objective_or_channel="SEARCH",
                budget_period="DAILY",
            ),
        )
        group = await _put(
            session,
            AdGroup(
                source="google",
                dataset_id=g,
                campaign_id=campaign.id,
                external_id="5001",
                name="Terms",
                status="ENABLED",
            ),
        )
        keywords: dict[str, int] = {}
        for criterion, name in enumerate(("ties", "single", "same_instant", "outside", "never")):
            row = await _put(
                session,
                Keyword(
                    source="google",
                    dataset_id=g,
                    ad_group_id=group.id,
                    criterion_id=str(7001 + criterion),
                    text=f"acme {name}",
                    match_type="EXACT",
                    status="ENABLED",
                ),
            )
            keywords[name] = row.id
        # Inserted out of time order, so snapshot ids do not follow captured_at: the earliest
        # instant's lower id carries quality 3 and the latest instant's higher id quality 5.
        for name, instant, quality in (
            ("ties", NY_UPPER, 6),
            ("ties", NY_LOWER, 3),
            ("ties", MID_NOON, 4),
            ("ties", NY_LOWER, 2),
            ("ties", NY_UPPER, 5),
            ("ties", NY_LOWER - SECOND, 1),
            ("ties", NY_UPPER + SECOND, 7),
            ("single", MID_06, 8),
            ("same_instant", MID_NOON, 9),
            ("same_instant", MID_NOON, 10),
            ("outside", NY_LOWER - SECOND, 11),
            ("outside", NY_UPPER + SECOND, 12),
        ):
            await _snapshot(
                session,
                "google",
                g,
                "keyword",
                keywords[name],
                instant,
                status="ENABLED",
                quality_score=quality,
            )
        await _snapshot(
            session, "google", old, "keyword", keywords["single"], MID_NOON, quality_score=13
        )
        await _snapshot(session, "google", g, "campaign", campaign.id, MID_NOON, status="active")

    async with sessions() as session:
        scope = _scope(session, window, thresholds, {"google": DatasetRef(g, "google", "live")})
        read = await metrics._load_keyword_observations(scope, GOOGLE_TZ)

    assert {
        key: [(item.captured_at, item.quality_score) for item in items]
        for key, items in read.items()
    } == {
        ("keyword", keywords["ties"]): [(NY_LOWER, 3), (NY_UPPER, 5)],
        ("keyword", keywords["single"]): [(MID_06, 8)],
        ("keyword", keywords["same_instant"]): [(MID_NOON, 10)],
    }
    assert list(read) == sorted(read)


@pytest.mark.anyio
async def test_discrepancy_shape_comes_from_the_latest_decision_else_the_inferred_shape(
    context: AnalysisContext, world: _World
) -> None:
    shapes = {item.key: item.shape for item in context.discrepancies}
    for key, shape in shapes.items():
        decision = context.decisions.get(key)
        if decision is not None:
            assert shape == decision.shape, key
    assert shapes[("campaign", world.camp_g1)] == "shared_member"
    assert ("campaign", world.camp_g2) not in context.decisions
    assert shapes[("campaign", world.camp_g2)] == "shared_member"
    assert shapes[("campaign", world.camp_g3)] == "own_budget"
    assert shapes[("budget", world.b_orphan)] == "budget"


@pytest.mark.anyio
async def test_budgets_are_read_with_an_empty_name(context: AnalysisContext) -> None:
    # context_from_world stores ``name=""`` for every budget, as the budget table has none.
    for budget in context.budgets.values():
        assert budget == BudgetInfo(
            budget.id,
            budget.external_id,
            "",
            budget.amount,
            budget.amount_micros,
            budget.period,
            budget.explicitly_shared,
            budget.currency,
        )


async def _google_ad_groups(session: AsyncSession, count: int) -> tuple[int, int, list[int]]:
    """Seed an active Google dataset with one campaign and ``count`` ad groups.

    Returns the dataset id, the campaign id and the ad group ids in ascending order.
    """
    dataset = await _dataset(session, "google", "live", "active")
    account = await _put(
        session,
        AdAccount(
            source="google",
            dataset_id=dataset,
            external_id="3334445555",
            currency="USD",
            timezone=GOOGLE_TZ,
        ),
    )
    campaign = await _put(
        session,
        Campaign(
            source="google",
            dataset_id=dataset,
            account_id=account.id,
            external_id="131",
            name="Order Search",
            status="active",
            raw_status="ENABLED",
            objective_or_channel="SEARCH",
        ),
    )
    groups: list[int] = []
    for position in range(count):
        group = await _put(
            session,
            AdGroup(
                source="google",
                dataset_id=dataset,
                campaign_id=campaign.id,
                external_id=f"53{position}",
                name=f"Order group {position}",
                status="ENABLED",
            ),
        )
        groups.append(group.id)
    return dataset, campaign.id, groups


def _scenario_search_term_key(row: SearchTermRow) -> tuple[int, bool, str, str]:
    """``context_from_world``'s search-term sort key (``_Converter._search_term_facts``)."""
    criterion = row.matched_criterion_id
    return (row.ad_group_id, criterion is None, criterion or "", row.search_term)


@pytest.mark.anyio
async def test_search_terms_list_in_the_scenario_order_on_any_database_locale(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    # Mixed case, embedded spaces, criterion ids of different lengths and criterion-less
    # terms (stored ''), inserted out of order. A linguistic collation such as en_US.utf8
    # ignores case and spaces at its first level and sorts '' first; context_from_world sorts
    # code points with the criterion-less rows last, and load_context must list the same.
    one_click = ("1", 1, 1, "0", None)
    async with sessions.begin() as session:
        google, campaign, (first, second) = await _google_ad_groups(session, 2)
        for group, criterion, term in (
            (second, "", "aaa"),
            (first, "7", "apple"),
            (first, "", "no keyword"),
            (first, "7", "acmea"),
            (second, "7", "beta"),
            (first, "7", "Bar"),
            (first, "", "Zed"),
            (first, "10", "zeta"),
            (first, "7", "acme b"),
        ):
            await _search_term(
                session, google, group, campaign, term, criterion, MID, "NONE", None, one_click
            )
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    loaded = [
        (row.ad_group_id, row.matched_criterion_id, row.search_term) for row in context.search_terms
    ]
    assert loaded == [
        (first, "10", "zeta"),
        (first, "7", "Bar"),
        (first, "7", "acme b"),
        (first, "7", "acmea"),
        (first, "7", "apple"),
        (first, None, "Zed"),
        (first, None, "no keyword"),
        (second, "7", "beta"),
        (second, None, "aaa"),
    ]
    assert list(context.search_terms) == sorted(context.search_terms, key=_scenario_search_term_key)


@pytest.mark.anyio
async def test_segments_list_in_the_scenario_order_on_any_database_locale(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    # context_from_world sorts a window's segment rows by campaign, then facet and value in
    # code-point order; values differing only in case must not follow the database locale.
    async with sessions.begin() as session:
        linkedin = await _dataset(session, "linkedin", "live", "active")
        account = await _put(
            session,
            AdAccount(source="linkedin", dataset_id=linkedin, external_id="52", currency="USD"),
        )
        campaigns: list[int] = []
        for external in ("761", "762"):
            campaign = await _put(
                session,
                Campaign(
                    source="linkedin",
                    dataset_id=linkedin,
                    account_id=account.id,
                    external_id=external,
                    name=f"Segments {external}",
                    status="active",
                    raw_status="ACTIVE",
                    objective_or_channel="LEAD_GENERATION",
                ),
            )
            campaigns.append(campaign.id)
        first, second = campaigns
        for campaign_id, facet, value in (
            (second, "industry", "urn:li:industry:4"),
            (first, "seniority", "urn:li:seniority:5"),
            (first, "job_function", "urn:li:function:a"),
            (first, "industry", "urn:li:industry:43"),
            (first, "job_function", "urn:li:function:B"),
            (first, "industry", "urn:li:industry:4"),
        ):
            await _put(
                session,
                FactSegmentWindow(
                    source="linkedin",
                    dataset_id=linkedin,
                    campaign_id=campaign_id,
                    facet=facet,
                    value=value,
                    window_start=START,
                    window_end=END,
                    impressions=100,
                    fetched_at=NOW,
                ),
            )
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    loaded = [(row.campaign_id, row.facet, row.value) for row in context.segments]
    assert loaded == [
        (first, "industry", "urn:li:industry:4"),
        (first, "industry", "urn:li:industry:43"),
        (first, "job_function", "urn:li:function:B"),
        (first, "job_function", "urn:li:function:a"),
        (first, "seniority", "urn:li:seniority:5"),
        (second, "industry", "urn:li:industry:4"),
    ]
    assert loaded == sorted(loaded)


@pytest.mark.anyio
async def test_crm_associations_fetches_and_window_deals_in_the_scenario_order(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    # context_from_world keys every CRM mapping in sorted key order, lists association
    # targets in code-point order and window deals by creation, then id in code-point order.
    # Ids of different lengths and letter case would sort otherwise under a numeric key or a
    # linguistic database collation.
    async with sessions.begin() as session:
        linkedin, campaign, creative = await _linkedin_world(session, "live")
        hubspot = await _dataset(session, "hubspot", "live", "active")
        lead = await _lead(session, linkedin, "resp-order", MID_NOON, campaign, creative)
        contact = await _contact(session, hubspot, "311", lifecycle_stage="lead")
        await _match(session, lead, hubspot, "matched", contact=contact.id, read="ok")
        for to_type, to_id in (
            ("meeting", "a7"),
            ("meeting", "9"),
            ("deal", "B2"),
            ("meeting", "B7"),
            ("deal", "a1"),
            ("meeting", "10"),
        ):
            await _associate(session, hubspot, "contact", "311", to_type, to_id)
        for deal_id, created_at in (
            ("a1", MID_NOON),
            ("B2", MID_NOON),
            ("10", MID_NOON),
            ("9", MID_06),
        ):
            await _deal(session, hubspot, deal_id, created_at)
            await _fetch(session, hubspot, ("deal", deal_id, "contact"), "ok_empty", None)
        await _fetch(session, hubspot, ("contact", "311", "meeting"), "ok", "partial", ("a7",))
        await _fetch(session, hubspot, ("contact", "311", "deal"), "ok", "ok")
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    crm = context.linkedin_crm
    assert crm is not None
    assert list(crm.associations.items()) == [
        (("contact", "311", "deal"), ("B2", "a1")),
        (("contact", "311", "meeting"), ("10", "9", "B7", "a7")),
    ]
    assert list(crm.fetches) == [
        ("contact", "311", "deal"),
        ("contact", "311", "meeting"),
        ("deal", "10", "contact"),
        ("deal", "9", "contact"),
        ("deal", "B2", "contact"),
        ("deal", "a1", "contact"),
    ]
    assert crm.window_deal_ids == ("9", "10", "B2", "a1")


@pytest.mark.anyio
async def test_google_touch_contacts_list_in_the_scenario_order_on_any_database_locale(
    sessions: Sessions, window: AnalysisWindow, thresholds: Thresholds
) -> None:
    # context_from_world orders Google-touch discovery by touch, then id in code-point order.
    # Ids sharing one touch whose letter case and length differ would sort otherwise under a
    # numeric key or a linguistic database collation (en_US.utf8 puts 'a1' before 'B2').
    async with sessions.begin() as session:
        hubspot = await _dataset(
            session, "hubspot", "live", "active", bindings=VALID_BINDINGS.to_json()
        )
        for hs_id, touch in (("a1", MID_NOON), ("10", MID_NOON), ("B2", MID_NOON), ("9", MID_06)):
            await _contact(
                session, hubspot, hs_id, google_campaign_ref="111", google_touch_at=touch
            )
    async with sessions() as session:
        context = await load_context(session, window, thresholds, kind="default", now=NOW)
    crm = context.google_crm
    assert crm is not None
    assert crm.bindings == VALID_BINDINGS
    discovered = [(contact.google_touch_at, contact.hs_id) for contact in crm.google_contacts]
    assert discovered == [(MID_06, "9"), (MID_NOON, "10"), (MID_NOON, "B2"), (MID_NOON, "a1")]
    assert discovered == sorted(discovered)
    assert list(crm.contacts) == ["10", "9", "B2", "a1"]
