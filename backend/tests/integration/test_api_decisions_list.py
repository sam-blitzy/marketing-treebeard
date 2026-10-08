"""Tests that ``GET /decisions`` lists the active datasets' decision log newest first.

``api/routes/decisions.list_decisions`` reads the whole log (or one subject type's) as the
newest ``limit`` rows of each active primary dataset, one bounded probe of
``ix_decision_dataset_decided`` per dataset, merged into one ``decided_at DESC, id DESC``
order and limit; one subject's history is read through ``ix_decision_subject_latest``. These
tests call the route function directly on real PostgreSQL and show that the page keeps the
log's contract: newest first with the ``id`` tie-break across and within datasets, a limit
that cuts the merged order (also when one dataset holds every newest row), only the active
datasets' rows, the ``subject_type`` and ``subject_id`` filters, the 422 for a
``subject_id`` without its type, and the joined subject views.

The tests run against the PostgreSQL 18 test database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``). The module creates a private schema, builds every table
of the model metadata in it, points every connection's ``search_path`` at it alone, and
drops it with ``CASCADE`` at teardown, so the ``public`` schema and every other schema stay
untouched. Each test runs inside one transaction that is rolled back afterwards. An
unreachable database fails the tests; it never skips them.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.api import schemas
from ads_engine.api.deps import ApiError, CurrentUser
from ads_engine.api.routes.decisions import DEFAULT_LIMIT, MAX_LIMIT, list_decisions
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Budget, Campaign
from ads_engine.db.models.decisions import Decision
from ads_engine.db.models.ops import SourceDataset

USER = CurrentUser(id=1, email="owner@example.com")
"""The signed-in user every request is made as; the route only requires one."""

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The instant the seeded decision timeline starts at."""

SHARED_BUDGET_EXTERNAL_ID = "9001"
"""Google id of the active dataset's explicitly shared budget."""

TIMELINE: tuple[tuple[str, str, str, int], ...] = (
    ("google_campaign_1", "google", "campaign", 0),
    ("linkedin_campaign_1", "linkedin", "campaign", 1),
    ("google_budget_1", "google", "budget", 2),
    ("retired_campaign_1", "retired", "campaign", 3),
    # Three decisions of one instant, inserted in id order: across and within datasets,
    # the higher id is listed first.
    ("google_member_1", "google", "member", 4),
    ("linkedin_campaign_2", "linkedin", "campaign", 4),
    ("google_campaign_2", "google", "campaign", 4),
    # The retired dataset's newest rows are newer than every active row.
    ("retired_campaign_2", "retired", "campaign", 10),
    ("retired_budget_1", "retired", "budget", 11),
    ("google_budget_2", "google", "budget", 5),
    ("google_campaign_3", "google", "campaign", 6),
    ("google_member_2", "google", "member", 7),
    ("google_campaign_4", "google", "campaign", 8),
)
"""Every seeded decision in insertion (``id``) order: label, dataset, subject, and the hours
after :data:`T0` it was decided at."""

ACTIVE_LOG: tuple[str, ...] = (
    "google_campaign_4",
    "google_member_2",
    "google_campaign_3",
    "google_budget_2",
    "google_campaign_2",
    "linkedin_campaign_2",
    "google_member_1",
    "google_budget_1",
    "linkedin_campaign_1",
    "google_campaign_1",
)
"""The active datasets' log, newest first: ``decided_at DESC``, then ``id DESC``."""

GOOGLE_NEWEST: int = 4
"""How many of the newest active rows belong to the Google dataset alone."""


@dataclass(frozen=True, slots=True)
class World:
    """The seeded datasets, subjects and decisions.

    Attributes:
        datasets: Dataset ids keyed ``google``, ``linkedin`` (both active) and ``retired``
            (a retired Google dataset).
        subjects: Subject ids keyed ``<dataset>:<subject>``; ``member`` is a Google campaign
            drawing from the shared budget.
        decisions: Decision ids keyed by their :data:`TIMELINE` label.
    """

    datasets: Mapping[str, int]
    subjects: Mapping[str, int]
    decisions: Mapping[str, int]

    def ids(self, labels: tuple[str, ...]) -> list[int]:
        """Return the decision ids of ``labels``, in order."""
        return [self.decisions[label] for label in labels]


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the decision log tests"
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
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards.

    Yields:
        The database URL and the private schema's name.
    """
    url = _database_url()
    schema = f"decisions_list_test_{uuid.uuid4().hex}"
    engine = sa.create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
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
async def session(database: tuple[URL, str]) -> AsyncIterator[AsyncSession]:
    """Yield an async session whose work is rolled back, on an engine of this test's loop."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            try:
                yield session
            finally:
                await session.rollback()
    finally:
        await engine.dispose()


async def _add[Row: Base](session: AsyncSession, row: Row) -> Row:
    """Insert ``row`` on its own, so ids follow the insertion order, and return it."""
    session.add(row)
    await session.flush()
    return row


async def _dataset(session: AsyncSession, source: str, state: str) -> int:
    """Insert a simulated primary dataset of ``source`` in ``state`` and return its id."""
    dataset = await _add(
        session, SourceDataset(source=source, mode="simulated", state=state, role="primary")
    )
    return dataset.id


async def _account(session: AsyncSession, source: str, dataset_id: int, currency: str) -> int:
    """Insert an ad account of ``dataset_id`` in ``currency`` and return its id."""
    account = await _add(
        session,
        AdAccount(
            source=source,
            dataset_id=dataset_id,
            external_id=f"{source}-{dataset_id}",
            name="Account",
            currency=currency,
        ),
    )
    return account.id


async def _budget(session: AsyncSession, dataset_id: int) -> int:
    """Insert an explicitly shared daily Google budget of ``dataset_id`` and return its id."""
    budget = await _add(
        session,
        Budget(
            source="google",
            dataset_id=dataset_id,
            external_id=SHARED_BUDGET_EXTERNAL_ID,
            amount=Decimal("75.000000"),
            amount_micros=75_000_000,
            period="DAILY",
            explicitly_shared=True,
        ),
    )
    return budget.id


async def _campaign(
    session: AsyncSession,
    source: str,
    dataset_id: int,
    account_id: int,
    *,
    name: str,
    budget_id: int | None = None,
) -> int:
    """Insert an active campaign of ``dataset_id`` and return its id.

    A campaign with ``budget_id`` draws from that shared budget and has no amount of its own.
    """
    campaign = await _add(
        session,
        Campaign(
            source=source,
            dataset_id=dataset_id,
            account_id=account_id,
            external_id=name.lower().replace(" ", "-"),
            name=name,
            status="active",
            raw_status="ENABLED" if source == "google" else "ACTIVE",
            budget_id=budget_id,
            daily_budget=None if budget_id is not None else Decimal("50.000000"),
            budget_period="DAILY" if source == "google" else None,
        ),
    )
    return campaign.id


def _decision_row(
    subjects: Mapping[str, int],
    datasets: Mapping[str, int],
    dataset: str,
    subject: str,
    decided_at: datetime,
) -> Decision:
    """Build the decision row of ``subject`` in ``dataset``, in the subject's own shape.

    Its ``patch`` is what a submission in that shape carries, amounts as submitted.
    """
    source = "linkedin" if dataset == "linkedin" else "google"
    patches = {"budget": {"daily_budget": "75.00"}, "member": {"status": "active"}}
    common = {
        "source": source,
        "dataset_id": datasets[dataset],
        "decided_at": decided_at,
        "decided_by": USER.email,
        "origin": "user",
        "patch": patches.get(subject, {"daily_budget": "50.00"}),
    }
    if subject == "budget":
        return Decision(
            **common,
            subject_type="budget",
            subject_id=subjects[f"{dataset}:budget"],
            shape="budget",
            daily_budget=Decimal("75.000000"),
        )
    if subject == "member":
        return Decision(
            **common,
            subject_type="campaign",
            subject_id=subjects[f"{dataset}:member"],
            shape="shared_member",
            status="active",
            budget_id=subjects[f"{dataset}:budget"],
        )
    return Decision(
        **common,
        subject_type="campaign",
        subject_id=subjects[f"{dataset}:campaign"],
        shape="own_budget",
        status="paused" if source == "linkedin" else "active",
        daily_budget=Decimal("50.000000"),
    )


async def _seed(session: AsyncSession) -> World:
    """Seed two active datasets, a retired one, their subjects and :data:`TIMELINE`."""
    datasets = {
        "google": await _dataset(session, "google", "active"),
        "linkedin": await _dataset(session, "linkedin", "active"),
        "retired": await _dataset(session, "google", "retired"),
    }
    subjects: dict[str, int] = {}
    for dataset in ("google", "retired"):
        account = await _account(session, "google", datasets[dataset], "USD")
        subjects[f"{dataset}:budget"] = await _budget(session, datasets[dataset])
        subjects[f"{dataset}:campaign"] = await _campaign(
            session, "google", datasets[dataset], account, name="Brand Search"
        )
        subjects[f"{dataset}:member"] = await _campaign(
            session,
            "google",
            datasets[dataset],
            account,
            name="Generic Search",
            budget_id=subjects[f"{dataset}:budget"],
        )
    linkedin_account = await _account(session, "linkedin", datasets["linkedin"], "EUR")
    subjects["linkedin:campaign"] = await _campaign(
        session, "linkedin", datasets["linkedin"], linkedin_account, name="Sponsored Content"
    )

    decisions: dict[str, int] = {}
    for label, dataset, subject, hours in TIMELINE:
        row = _decision_row(subjects, datasets, dataset, subject, T0 + timedelta(hours=hours))
        decisions[label] = (await _add(session, row)).id
    return World(datasets=datasets, subjects=subjects, decisions=decisions)


def _ids(views: list[schemas.Decision]) -> list[int]:
    """Return the decision ids of ``views``, in order."""
    return [view.id for view in views]


@pytest.mark.anyio
@pytest.mark.db
async def test_the_log_lists_the_active_datasets_newest_first(session: AsyncSession) -> None:
    """The default page holds every active decision, ordered by ``decided_at`` and then
    ``id``, both descending, across and within datasets; the retired dataset's rows, though
    newer than all of them, never appear."""
    world = await _seed(session)

    views = await list_decisions(USER, session)

    assert _ids(views) == world.ids(ACTIVE_LOG)
    assert len(ACTIVE_LOG) < DEFAULT_LIMIT
    instants = [view.decided_at for view in views]
    assert instants == sorted(instants, reverse=True)
    retired = {world.decisions[label] for label, dataset, _, _ in TIMELINE if dataset == "retired"}
    assert retired.isdisjoint(_ids(views))


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("limit", [1, 2, GOOGLE_NEWEST, GOOGLE_NEWEST + 1, 6, 7, 10, MAX_LIMIT])
async def test_the_limit_cuts_the_merged_log(session: AsyncSession, limit: int) -> None:
    """``limit`` keeps the newest rows of all active datasets together: the first four come
    from the Google dataset alone, the fifth to seventh share one instant across both
    datasets, and a limit beyond the log returns all of it."""
    world = await _seed(session)

    views = await list_decisions(USER, session, limit=limit)

    assert _ids(views) == world.ids(ACTIVE_LOG[:limit])


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("subject_type", "limit", "expected"),
    [
        ("budget", DEFAULT_LIMIT, ("google_budget_2", "google_budget_1")),
        ("budget", 1, ("google_budget_2",)),
        (
            "campaign",
            DEFAULT_LIMIT,
            tuple(label for label in ACTIVE_LOG if "budget" not in label),
        ),
        (
            "campaign",
            5,
            (
                "google_campaign_4",
                "google_member_2",
                "google_campaign_3",
                "google_campaign_2",
                "linkedin_campaign_2",
            ),
        ),
    ],
    ids=["budget", "budget-limit-1", "campaign", "campaign-limit-5"],
)
async def test_subject_type_restricts_the_log(
    session: AsyncSession,
    subject_type: schemas.SubjectType,
    limit: int,
    expected: tuple[str, ...],
) -> None:
    """``subject_type`` lists only that type's active decisions, newest first, and the limit
    cuts the filtered order rather than the whole log's."""
    world = await _seed(session)

    views = await list_decisions(USER, session, subject_type=subject_type, limit=limit)

    assert _ids(views) == world.ids(expected)
    assert {view.subject.type for view in views} == {subject_type}


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("subject", "limit", "expected"),
    [
        (
            "google:campaign",
            DEFAULT_LIMIT,
            ("google_campaign_4", "google_campaign_3", "google_campaign_2", "google_campaign_1"),
        ),
        ("google:campaign", 2, ("google_campaign_4", "google_campaign_3")),
        ("google:member", DEFAULT_LIMIT, ("google_member_2", "google_member_1")),
        ("google:budget", DEFAULT_LIMIT, ("google_budget_2", "google_budget_1")),
        ("linkedin:campaign", DEFAULT_LIMIT, ("linkedin_campaign_2", "linkedin_campaign_1")),
        ("retired:campaign", DEFAULT_LIMIT, ()),
        ("retired:budget", DEFAULT_LIMIT, ()),
    ],
    ids=[
        "campaign",
        "campaign-limit-2",
        "shared-member",
        "budget",
        "linkedin-campaign",
        "retired-campaign",
        "retired-budget",
    ],
)
async def test_subject_id_lists_one_subjects_history(
    session: AsyncSession, subject: str, limit: int, expected: tuple[str, ...]
) -> None:
    """``subject_type`` with ``subject_id`` lists that subject's active history, newest
    first; a subject of the retired dataset lists nothing."""
    world = await _seed(session)
    subject_type: schemas.SubjectType = "budget" if subject.endswith(":budget") else "campaign"

    views = await list_decisions(
        USER,
        session,
        subject_type=subject_type,
        subject_id=world.subjects[subject],
        limit=limit,
    )

    assert _ids(views) == world.ids(expected)
    assert {(view.subject.type, view.subject.id) for view in views} <= {
        (subject_type, world.subjects[subject])
    }


@pytest.mark.anyio
@pytest.mark.db
async def test_subject_id_without_subject_type_is_rejected(session: AsyncSession) -> None:
    """Subject ids are unique only within a subject type, so an id alone is a 422."""
    world = await _seed(session)

    with pytest.raises(ApiError) as caught:
        await list_decisions(USER, session, subject_id=world.subjects["google:campaign"])

    assert caught.value.status_code == 422
    assert caught.value.code == "invalid_query"


@pytest.mark.anyio
@pytest.mark.db
async def test_no_active_dataset_lists_nothing(session: AsyncSession) -> None:
    """With no active primary dataset the log is empty, whatever a retired dataset holds."""
    retired = await _dataset(session, "google", "retired")
    account = await _account(session, "google", retired, "USD")
    campaign = await _campaign(session, "google", retired, account, name="Brand Search")
    await _add(
        session,
        Decision(
            source="google",
            dataset_id=retired,
            subject_type="campaign",
            subject_id=campaign,
            shape="own_budget",
            status="active",
            daily_budget=Decimal("50.000000"),
            decided_at=T0,
            origin="baseline",
        ),
    )

    assert await list_decisions(USER, session) == []
    assert await list_decisions(USER, session, subject_type="campaign") == []
    assert await list_decisions(USER, session, subject_type="campaign", subject_id=campaign) == []


@pytest.mark.anyio
@pytest.mark.db
async def test_each_row_carries_its_subjects_view(session: AsyncSession) -> None:
    """Every listed row joins its own subject: a campaign by name with its account's
    currency, a shared budget as ``Shared budget {external_id}`` in its members' currency."""
    world = await _seed(session)

    views = {view.id: view for view in await list_decisions(USER, session)}

    expected = {
        "google_campaign_4": schemas.SubjectView(
            type="campaign",
            id=world.subjects["google:campaign"],
            name="Brand Search",
            source="google",
            currency="USD",
        ),
        "google_member_2": schemas.SubjectView(
            type="campaign",
            id=world.subjects["google:member"],
            name="Generic Search",
            source="google",
            currency="USD",
        ),
        "google_budget_2": schemas.SubjectView(
            type="budget",
            id=world.subjects["google:budget"],
            name=f"Shared budget {SHARED_BUDGET_EXTERNAL_ID}",
            source="google",
            currency="USD",
        ),
        "linkedin_campaign_2": schemas.SubjectView(
            type="campaign",
            id=world.subjects["linkedin:campaign"],
            name="Sponsored Content",
            source="linkedin",
            currency="EUR",
        ),
    }
    hours = {label: offset for label, _, _, offset in TIMELINE}
    for label, subject in expected.items():
        view = views[world.decisions[label]]
        assert view.subject == subject
        assert view.decided_at == T0 + timedelta(hours=hours[label])
    member = views[world.decisions["google_member_2"]]
    assert (member.shape, member.budget_id) == ("shared_member", world.subjects["google:budget"])
    budget = views[world.decisions["google_budget_2"]]
    assert (budget.shape, budget.status, budget.daily_budget) == (
        "budget",
        None,
        Decimal("75.000000"),
    )
    assert budget.model_dump(mode="json")["patch"] == {"daily_budget": "75.00"}
    assert budget.decided_by == USER.email
