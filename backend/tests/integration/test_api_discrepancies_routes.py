"""HTTP tests for ``GET /discrepancies`` paging (``ads_engine.api.routes.discrepancies``).

The route lists the C0 discrepancies of the active datasets oldest ``detected_at`` first,
then by id (AAP 0.5.4.4), as a ``Discrepancy[]`` body (AAP 0.5.3). It reads at most
``limit`` rows (1 to 500, default 200) per page, continues after the ``after`` cursor's
``(detected_at, id)`` position, and names the next page in an RFC 8288 ``Link: rel="next"``
header whenever it read a full page of rows. These tests drive it over HTTP through
``httpx.ASGITransport`` and check that following the ``Link`` header returns every row
exactly once, in order, across ``detected_at`` ties, resolved cursor rows and rows whose
subject is unavailable, and that bad paging parameters are 422s, never database errors.

``api/app.py`` does not exist yet, so :func:`_app` builds the smallest application that
mounts the router the way it will: under ``/api/v1`` with the CSRF dependency, with handlers
that render ``ApiError``, request validation failures and any other exception in the AAP
error shape ``{"error": {"code", "message"}}``. Authentication is not under test here, so
``require_user`` is overridden with a fixed admin; ``get_session`` is overridden with
sessions on the test schema.

The database is the PostgreSQL 18 test database named by ``TEST_DATABASE_URL`` (falling
back to ``DATABASE_URL``). The module creates a private schema, builds every table of the
model metadata in it, points every connection's ``search_path`` at it alone, and drops it
with ``CASCADE`` at teardown, so the ``public`` schema and every other schema stay
untouched. Each test seeds its own LinkedIn world, whose new active dataset retires the
previous test's. An unreachable database fails the tests; it never skips them.
"""

import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.api import deps
from ads_engine.api.deps import ApiError, CurrentUser, require_csrf
from ads_engine.api.routes import discrepancies
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Campaign
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.ops import SourceDataset
from ads_engine.sync.snapshots import SnapshotRow, ensure_baseline, write_snapshots

type Sessions = async_sessionmaker[AsyncSession]

BASE_URL = "http://127.0.0.1"
"""The origin the in-process client sends requests to; nothing listens on it."""

PATH = "/api/v1/discrepancies"
"""The route under test, with the prefix ``api/app.py`` mounts it under."""

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The baseline entity sync; every ``detected_at`` is whole hours after it."""

UNAVAILABLE_CAMPAIGN_ID = 9_000_000_000
"""A campaign id no row has, so the domain reports the subject not found."""

_LINK = re.compile(r'<(?P<target>[^<>]+)>; rel="next"')
"""The one ``Link`` value the route sends: a relative reference with ``rel="next"``."""

UNRESOLVED_ORDER = ("a", "b", "tie_low", "tie_high", "c", "d")
"""The unresolved discrepancies by name, in the order the route must list them."""

UNRESOLVED_INSERT_ORDER = ("d", "b", "tie_low", "a", "tie_high", "c")
"""The order they are inserted in, so their ids do not follow their ``detected_at``.

``tie_low`` is inserted before ``tie_high`` and both share one ``detected_at``, so the id
tie-break must put ``tie_low`` first.
"""

UNRESOLVED_SPEC: dict[str, tuple[float, str, str]] = {
    "a": (1, "open", "status"),
    "b": (2, "awaiting_platform", "budget"),
    "tie_low": (3, "open", "status"),
    "tie_high": (3, "open", "budget"),
    "c": (4, "awaiting_platform", "status"),
    "d": (5, "open", "status"),
}
"""Per name: hours after :data:`T0` it was detected, its state and its kind."""

RESOLVED_ORDER = ("r1", "r2", "r3", "r4", "r5")
"""The resolved discrepancies by name, oldest first."""

RESOLVED_INSERT_ORDER = ("r4", "r1", "r5", "r3", "r2")
"""The order they are inserted in, so their ids do not follow their ``detected_at``."""


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the discrepancy route tests"
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
    schema = f"api_discrepancies_test_{uuid.uuid4().hex}"
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
async def sessions(database: tuple[URL, str]) -> AsyncIterator[Sessions]:
    """Yield a session factory on an engine created and disposed inside this test's loop."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------
# The application under test
# ---------------------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Render the AAP 0.5.3 error shape."""
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an ``ApiError`` with its status, code, message and headers."""
    if not isinstance(exc, ApiError):
        raise TypeError(f"the ApiError handler received {type(exc).__name__}")
    response = _error(exc.status_code, exc.code, exc.message)
    response.headers.update(exc.headers or {})
    return response


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Render a request validation failure as 422 ``validation_error``."""
    return _error(422, "validation_error", "The request is not valid")


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Render any other exception, a database error included, as 500 ``internal_error``."""
    return _error(500, "internal_error", "Internal server error")


def _app(sessions: Sessions) -> FastAPI:
    """Build an app that mounts the discrepancy router the way ``api/app.py`` will.

    The router is included under ``/api/v1`` with the CSRF dependency. ``get_session``
    yields sessions on the test schema, and ``require_user`` returns a fixed admin.
    """
    app = FastAPI()
    app.include_router(discrepancies.router, prefix="/api/v1", dependencies=[Depends(require_csrf)])

    async def session_override() -> AsyncIterator[AsyncSession]:
        """Yield one session on the test schema per request, closed afterwards."""
        async with sessions() as session:
            yield session

    def user_override() -> CurrentUser:
        """Return the signed-in admin without a session cookie."""
        return CurrentUser(id=1, email="admin@example.com")

    app.dependency_overrides[deps.get_session] = session_override
    app.dependency_overrides[deps.require_user] = user_override
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
    return app


@pytest.fixture
async def client(sessions: Sessions) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client wired in-process to :func:`_app`.

    ``raise_app_exceptions=False`` lets an unexpected server error come back as the 500
    response the catch-all handler renders, so a test sees the status instead of a raise.
    """
    transport = httpx.ASGITransport(app=_app(sessions), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        yield http


# ---------------------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """One test's LinkedIn data: discrepancy ids by name and what they belong to.

    Attributes:
        dataset_id: The active LinkedIn primary dataset.
        retired_dataset_id: The dataset it replaced, which holds :attr:`retired_open`.
        unresolved: The unresolved discrepancies of the active dataset, by name.
        resolved: The resolved discrepancies of the active dataset, by name.
        retired_open: An open discrepancy of the retired dataset, detected before every
            other one, which is never listed.
    """

    dataset_id: int
    retired_dataset_id: int
    unresolved: dict[str, int]
    resolved: dict[str, int]
    retired_open: int

    def unresolved_ids(self) -> list[int]:
        """Return the unresolved ids in the order the route must list them."""
        return [self.unresolved[name] for name in UNRESOLVED_ORDER]

    def resolved_ids(self) -> list[int]:
        """Return the resolved ids in the order the route must list them."""
        return [self.resolved[name] for name in RESOLVED_ORDER]


def _hours(hours: float) -> datetime:
    """Return the instant ``hours`` after :data:`T0`."""
    return T0 + timedelta(hours=hours)


async def _new_active_dataset(session: AsyncSession) -> int:
    """Insert an active LinkedIn primary dataset, retiring the previous active one."""
    await session.execute(
        update(SourceDataset)
        .where(
            SourceDataset.source == "linkedin",
            SourceDataset.state == "active",
            SourceDataset.role == "primary",
        )
        .values(state="retired", retired_at=T0)
    )
    dataset = SourceDataset(source="linkedin", mode="simulated", state="active", role="primary")
    session.add(dataset)
    await session.flush()
    return dataset.id


async def _campaigns(session: AsyncSession, dataset_id: int, count: int) -> list[int]:
    """Insert a USD LinkedIn account with ``count`` own-budget campaigns; return their ids."""
    account = AdAccount(
        source="linkedin",
        dataset_id=dataset_id,
        external_id=f"5{dataset_id:08d}",
        name="Acme LinkedIn",
        currency="USD",
        timezone="UTC",
    )
    session.add(account)
    await session.flush()
    ids: list[int] = []
    for number in range(count):
        campaign = Campaign(
            source="linkedin",
            dataset_id=dataset_id,
            account_id=account.id,
            external_id=f"urn:li:sponsoredCampaign:{dataset_id}{number:03d}",
            name=f"Campaign {number}",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("25.00"),
        )
        session.add(campaign)
        await session.flush()
        ids.append(campaign.id)
    return ids


async def _baseline(
    session: AsyncSession, dataset_id: int, campaign_ids: Sequence[int]
) -> dict[int, tuple[int, int]]:
    """Snapshot each campaign at :data:`T0` and write its baseline ``own_budget`` decision.

    Returns:
        Per campaign id, its snapshot id and its baseline decision id.
    """
    rows = [
        SnapshotRow.campaign(
            source="linkedin",
            entity_id=campaign_id,
            status="active",
            raw_status="ACTIVE",
            daily_budget=Decimal("25.00"),
            campaign_group_urn="urn:li:sponsoredCampaignGroup:5",
        )
        for campaign_id in campaign_ids
    ]
    snapshot_ids = await write_snapshots(session, dataset_id, rows, captured_at=T0)
    written = await ensure_baseline(session, dataset_id, captured_at=T0, sealed=False)
    assert written == len(campaign_ids)
    decisions = {
        decision.subject_id: decision
        for decision in await session.scalars(
            select(Decision).where(Decision.dataset_id == dataset_id)
        )
    }
    assert {decision.shape for decision in decisions.values()} == {"own_budget"}
    return {
        campaign_id: (snapshot_ids[("campaign", campaign_id)], decisions[campaign_id].id)
        for campaign_id in campaign_ids
    }


async def _discrepancy(
    session: AsyncSession,
    dataset_id: int,
    subject_id: int,
    *,
    kind: str,
    state: str,
    detected_at: datetime,
    ids: tuple[int, int] | None = None,
) -> int:
    """Insert one campaign discrepancy and return its id.

    Args:
        session: The seeding session.
        dataset_id: The dataset it belongs to.
        subject_id: The campaign it is about; it need not exist.
        kind: ``status`` or ``budget``.
        state: ``open``, ``awaiting_platform`` or ``resolved``.
        detected_at: When it was detected.
        ids: The snapshot and decision it compared, or ``None`` for neither.
    """
    if kind == "status":
        decided: dict[str, object] = {"status": "active"}
        platform: dict[str, object] = {"status": "paused"}
    else:
        decided = {"daily_budget": "25.00"}
        platform = {"daily_budget": "30.00"}
    snapshot_id, decision_id = (None, None) if ids is None else ids
    resolved = state == "resolved"
    row = Discrepancy(
        source="linkedin",
        dataset_id=dataset_id,
        subject_type="campaign",
        subject_id=subject_id,
        kind=kind,
        decided_value=decided,
        platform_value=platform,
        snapshot_id=snapshot_id,
        decision_id=decision_id,
        detected_at=detected_at,
        state=state,
        resolved_at=detected_at + timedelta(minutes=30) if resolved else None,
        resolution="platform_correct" if resolved else None,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _world(sessions: Sessions) -> World:
    """Commit one test's datasets, campaigns, baselines and discrepancies.

    * The first dataset gets a campaign with an open discrepancy detected before every
      other one, then is retired by the second dataset's activation.
    * The active dataset gets six campaigns, each with a snapshot and a baseline
      ``own_budget`` decision; six unresolved discrepancies, two of them sharing one
      ``detected_at``; and five resolved ones. Both sets are inserted out of order, so ids
      alone never give the listing order.
    """
    async with sessions() as session, session.begin():
        retired_dataset_id = await _new_active_dataset(session)
        (retired_campaign,) = await _campaigns(session, retired_dataset_id, 1)
        retired_open = await _discrepancy(
            session,
            retired_dataset_id,
            retired_campaign,
            kind="status",
            state="open",
            detected_at=_hours(-100),
        )
        dataset_id = await _new_active_dataset(session)
        campaign_ids = await _campaigns(session, dataset_id, len(UNRESOLVED_ORDER))
        baselines = await _baseline(session, dataset_id, campaign_ids)
        subject_of = dict(zip(UNRESOLVED_ORDER, campaign_ids, strict=True))
        unresolved: dict[str, int] = {}
        for name in UNRESOLVED_INSERT_ORDER:
            hours, state, kind = UNRESOLVED_SPEC[name]
            unresolved[name] = await _discrepancy(
                session,
                dataset_id,
                subject_of[name],
                kind=kind,
                state=state,
                detected_at=_hours(hours),
                ids=baselines[subject_of[name]],
            )
        resolved: dict[str, int] = {}
        for name in RESOLVED_INSERT_ORDER:
            position = RESOLVED_ORDER.index(name)
            subject = campaign_ids[position]
            resolved[name] = await _discrepancy(
                session,
                dataset_id,
                subject,
                kind="status",
                state="resolved",
                detected_at=_hours(-50 + 10 * position),
                ids=baselines[subject],
            )
    assert unresolved["tie_low"] < unresolved["tie_high"]
    return World(
        dataset_id=dataset_id,
        retired_dataset_id=retired_dataset_id,
        unresolved=unresolved,
        resolved=resolved,
        retired_open=retired_open,
    )


@pytest.fixture
async def world(sessions: Sessions) -> World:
    """Seed this test's world (:func:`_world`)."""
    return await _world(sessions)


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _next_target(response: httpx.Response) -> str | None:
    """Return the ``Link: rel="next"`` target, or ``None`` when the header is absent.

    The target must be a relative reference to the route itself: no scheme, no host.
    """
    header = response.headers.get("link")
    if header is None:
        return None
    match = _LINK.fullmatch(header)
    assert match is not None, f"unexpected Link header {header!r}"
    target = match["target"]
    parts = urlsplit(target)
    assert (parts.scheme, parts.netloc, parts.path) == ("", "", PATH)
    return target


def _query(target: str) -> list[tuple[str, str]]:
    """Return a target's query parameters in order, repeated keys kept."""
    return parse_qsl(urlsplit(target).query, keep_blank_values=True)


def _ids(response: httpx.Response) -> list[int]:
    """Return the ids of a successful list response, in body order."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return [item["id"] for item in body]


async def _walk(http: httpx.AsyncClient, first: str) -> tuple[list[int], list[httpx.Response]]:
    """Follow ``Link: rel="next"`` from ``first`` until a page carries no header.

    Returns:
        Every id listed, in page order, and every page response.
    """
    ids: list[int] = []
    pages: list[httpx.Response] = []
    target: str | None = first
    while target is not None:
        assert len(pages) <= 50, "the Link chain does not end"
        response = await http.get(target)
        ids.extend(_ids(response))
        pages.append(response)
        target = _next_target(response)
    return ids, pages


def _assert_error(response: httpx.Response, status_code: int, code: str) -> None:
    """Assert an error response in the AAP shape with the given status and code."""
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]


# ---------------------------------------------------------------------------------------
# Listing and ordering
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_default_lists_unresolved_oldest_first_without_link(
    world: World, client: httpx.AsyncClient
) -> None:
    """Without parameters, every unresolved row of the active dataset, oldest first."""
    response = await client.get(PATH)
    assert _ids(response) == world.unresolved_ids()
    assert world.retired_open not in _ids(response)
    assert "link" not in response.headers
    body = response.json()
    assert {item["state"] for item in body} == {"open", "awaiting_platform"}
    first = body[0]
    assert first["shape"] == "own_budget"
    assert first["subject"]["currency"] == "USD"
    assert first["resolutions"] == ["platform_correct", "decision_correct"]


@pytest.mark.anyio
async def test_limit_reads_first_rows_and_links_the_next_page(
    world: World, client: httpx.AsyncClient
) -> None:
    """``limit=3`` returns the first three rows and a ``Link`` that keeps every parameter."""
    response = await client.get(
        PATH, params=[("state", "open"), ("state", "awaiting_platform"), ("limit", "3")]
    )
    expected = world.unresolved_ids()
    assert _ids(response) == expected[:3]
    target = _next_target(response)
    assert target is not None
    assert _query(target) == [
        ("state", "open"),
        ("state", "awaiting_platform"),
        ("limit", "3"),
        ("after", str(expected[2])),
    ]


@pytest.mark.anyio
async def test_link_replaces_an_earlier_after(world: World, client: httpx.AsyncClient) -> None:
    """A page reached through ``after`` links onward with ``after`` replaced, not repeated."""
    expected = world.unresolved_ids()
    response = await client.get(PATH, params={"limit": 2, "after": expected[1]})
    assert _ids(response) == expected[2:4]
    target = _next_target(response)
    assert target is not None
    assert _query(target) == [("limit", "2"), ("after", str(expected[3]))]


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 7, 500])
async def test_following_links_lists_every_row_once_in_order(
    limit: int, world: World, client: httpx.AsyncClient
) -> None:
    """Following ``Link`` lists every unresolved row once in order, across the tie."""
    ids, pages = await _walk(client, f"{PATH}?limit={limit}")
    assert ids == world.unresolved_ids()
    assert len(ids) == len(set(ids))
    rows = len(UNRESOLVED_ORDER)
    # A full last page cannot tell that nothing follows, so it links to an empty page.
    assert len(pages) == rows // limit + 1
    assert all(len(_ids(page)) == limit for page in pages[:-1])
    assert "link" not in pages[-1].headers


@pytest.mark.anyio
async def test_resolved_history_pages_oldest_first(world: World, client: httpx.AsyncClient) -> None:
    """``state=resolved&limit=2`` pages through the whole resolved history."""
    ids, pages = await _walk(client, f"{PATH}?state=resolved&limit=2")
    assert ids == world.resolved_ids()
    assert [len(_ids(page)) for page in pages] == [2, 2, 1]
    for page in pages[:-1]:
        target = _next_target(page)
        assert target is not None
        assert ("state", "resolved") in _query(target)


@pytest.mark.anyio
async def test_mixed_states_page_through_both(world: World, client: httpx.AsyncClient) -> None:
    """Repeated ``state`` values page through the union in one ``(detected_at, id)`` order."""
    ids, _ = await _walk(client, f"{PATH}?state=resolved&state=open&limit=4")
    open_names = [name for name in UNRESOLVED_ORDER if UNRESOLVED_SPEC[name][1] == "open"]
    assert ids == world.resolved_ids() + [world.unresolved[name] for name in open_names]


# ---------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("limit", ["0", "-1", "501", "two"])
async def test_limit_outside_bounds_is_422(
    limit: str, world: World, client: httpx.AsyncClient
) -> None:
    """``limit`` below 1, above 500 or not an integer is a validation error."""
    _assert_error(await client.get(PATH, params={"limit": limit}), 422, "validation_error")


@pytest.mark.anyio
async def test_limit_at_its_maximum_is_accepted(world: World, client: httpx.AsyncClient) -> None:
    """``limit=500`` is the largest page."""
    assert discrepancies.MAX_LIMIT == 500
    assert discrepancies.DEFAULT_LIMIT == 200
    assert _ids(await client.get(PATH, params={"limit": 500})) == world.unresolved_ids()


@pytest.mark.anyio
@pytest.mark.parametrize("after", [str(2**63), str(2**64), "0", "-5", "first"])
async def test_after_outside_bigint_ids_is_422(
    after: str, world: World, client: httpx.AsyncClient
) -> None:
    """An ``after`` that cannot be a ``bigint`` id is a validation error, not a 500."""
    _assert_error(await client.get(PATH, params={"after": after}), 422, "validation_error")


@pytest.mark.anyio
async def test_after_naming_no_discrepancy_is_invalid_cursor(
    world: World, sessions: Sessions, client: httpx.AsyncClient
) -> None:
    """An ``after`` no discrepancy has is 422 ``invalid_cursor``, whatever the range."""
    async with sessions() as session:
        highest = await session.scalar(select(func.max(Discrepancy.id)))
    assert highest is not None
    for after in (highest + 1000, 2**63 - 1):
        response = await client.get(PATH, params={"after": after})
        _assert_error(response, 422, "invalid_cursor")
        assert str(after) not in response.json()["error"]["message"]


# ---------------------------------------------------------------------------------------
# Cursor stability
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cursor_row_resolved_after_first_page_still_continues(
    world: World, sessions: Sessions, client: httpx.AsyncClient
) -> None:
    """A cursor whose row was resolved after its page was served keeps its position."""
    expected = world.unresolved_ids()
    first = await client.get(PATH, params={"limit": 3})
    assert _ids(first) == expected[:3]
    target = _next_target(first)
    assert target is not None
    cursor_id = expected[2]
    async with sessions() as session, session.begin():
        await session.execute(
            update(Discrepancy)
            .where(Discrepancy.id == cursor_id)
            .values(state="resolved", resolved_at=_hours(10), resolution="platform_correct")
        )
    rest, _ = await _walk(client, target)
    assert rest == expected[3:]


@pytest.mark.anyio
async def test_cursor_from_another_dataset_positions_the_page(
    world: World, client: httpx.AsyncClient
) -> None:
    """The cursor row is found whatever its dataset, and its row is never listed."""
    response = await client.get(PATH, params={"after": world.retired_open})
    assert _ids(response) == world.unresolved_ids()
    tie_cursor = await client.get(PATH, params={"after": world.unresolved["tie_low"]})
    assert _ids(tie_cursor) == [world.unresolved[name] for name in ("tie_high", "c", "d")]


# ---------------------------------------------------------------------------------------
# Rows whose subject is unavailable
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_page_of_unavailable_subjects_still_links_onward(
    world: World, sessions: Sessions, client: httpx.AsyncClient
) -> None:
    """A full read of rows links onward even when every row's subject is unavailable."""
    async with sessions() as session, session.begin():
        unavailable = [
            await _discrepancy(
                session,
                world.dataset_id,
                UNAVAILABLE_CAMPAIGN_ID + offset,
                kind="status",
                state="open",
                detected_at=_hours(-1 + offset / 10),
            )
            for offset in range(3)
        ]
    first = await client.get(PATH, params={"limit": 3})
    assert _ids(first) == []
    target = _next_target(first)
    assert target is not None
    assert _query(target) == [("limit", "3"), ("after", str(unavailable[-1]))]
    ids, _ = await _walk(client, f"{PATH}?limit=3")
    assert ids == world.unresolved_ids()


@pytest.mark.anyio
async def test_short_page_with_unavailable_subject_ends_the_list(
    world: World, sessions: Sessions, client: httpx.AsyncClient
) -> None:
    """A read of fewer rows than ``limit`` carries no ``Link``, skipped rows or not."""
    async with sessions() as session, session.begin():
        # Another subject than the previous test's: the one-unresolved-row-per-subject index
        # spans datasets, and that test's rows stay open in its now-retired dataset.
        await _discrepancy(
            session,
            world.dataset_id,
            UNAVAILABLE_CAMPAIGN_ID + 10,
            kind="status",
            state="open",
            detected_at=_hours(6),
        )
    response = await client.get(PATH, params={"limit": len(UNRESOLVED_ORDER) + 2})
    assert _ids(response) == world.unresolved_ids()
    assert "link" not in response.headers
