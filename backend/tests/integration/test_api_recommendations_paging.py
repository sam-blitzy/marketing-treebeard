"""Integration tests: a page carries each group's first items; ``/recommendations/items`` the rest.

AAP 0.1.1 (the five sections render from precomputed runs in under 2 s), 0.5.3
(``GET /recommendations?run_id=`` returning ``RecommendationsPage``; authentication and the error
shape) and 0.5.2 / 0.5.4.2 (feedback hides the same rule and entity until ``until``; C0 is exempt).
Every request goes through ``create_app()`` and ``httpx.ASGITransport`` against the PostgreSQL 18
test database. The guarantees proven here:

* **Bounded groups**: each group of the page carries at most ``GROUP_PAGE_SIZE`` items, its first
  in stored rank order (rank, then id), with ``total`` counting every item the run stored in it
  and ``hidden`` the items feedback in force now hides (never a C0). Every static group is
  present, empty ones included, and dynamic groups follow by their lowest rank.
* **Every item reachable**: ``GET /recommendations/items`` continues a group from any offset in
  the same order, so paging from the page's item count visits every stored item exactly once,
  each with the ``current`` overlay the page would give it.
* **Errors**: 401 without a session, 404 for an unknown run, 409 ``run_not_ready`` for a run
  that is not succeeded, and 422 for a parameter outside its bounds, all in the
  ``{"error": {"code", "message"}}`` shape.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.analysis.context import C0, GROUP_ORDER, K1, K2, SECTION_ORDER
from ads_engine.api import schemas
from ads_engine.api.app import API_PREFIX
from ads_engine.db.models.analysis import AnalysisRun, Recommendation, RecommendationFeedback
from ads_engine.db.models.ops import SourceDataset

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

NOW: Final = datetime.now(UTC)
"""The seed's clock: feedback in force is judged against the API's real ``now``."""

WINDOW_START: Final = date(2026, 9, 9)
WINDOW_END: Final = date(2026, 10, 6)

BLOCK_ITEMS: Final = 250
"""How many items the seeded Keywords ``block`` group holds: two and a half pages."""

HIDDEN_BLOCK_RANKS: Final = (3, 150, 240)
"""The ranks of the ``block`` items dismissed with feedback still in force."""

EXPIRED_BLOCK_RANK: Final = 4
"""The rank of the ``block`` item whose feedback has expired: shown."""

TIED_RANK: Final = 120
"""A rank two ``block`` items share, ordered by id."""


# ---------------------------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seeded:
    """The committed run of the paging tests.

    Attributes:
        run_id: The succeeded run.
        block_order: The ids of its ``block`` items in stored order (rank, then id).
        hidden_block: The ids of the ``block`` items feedback hides now.
        expand_hidden: The id of the hidden ``expand`` item.
        c0_id: The id of the C0 item, given feedback that never hides it.
    """

    run_id: int
    block_order: tuple[int, ...]
    hidden_block: frozenset[int]
    expand_hidden: int
    c0_id: int


def _evidence(provenance: Sequence[Mapping[str, Any]], *, name: str) -> dict[str, Any]:
    """Return a stored item's frozen evidence, naming its entity as the runner does."""
    return {
        "window": {
            "start": WINDOW_START.isoformat(),
            "end": WINDOW_END.isoformat(),
            "days": (WINDOW_END - WINDOW_START).days + 1,
            "source_days": [],
        },
        "metrics": [
            {"key": "cost", "label": "Cost", "value": 12.5, "unit": "currency", "currency": "USD"}
        ],
        "baselines": [],
        "thresholds": {},
        "datasets": [],
        "provenance": [dict(entry) for entry in provenance],
        "extra": {"entity": {"source": "google", "type": "search_term", "name": name}},
    }


def _recommendation(
    run_id: int,
    provenance: Sequence[Mapping[str, Any]],
    *,
    section: str,
    group: str,
    rank: int,
    rule_id: str = K1,
    entity: str,
) -> Recommendation:
    """Return one unsaved stored item of ``run_id``."""
    entity_type = "campaign" if rule_id == C0 else "search_term"
    return Recommendation(
        run_id=run_id,
        section=section,
        group=group,
        rule_id=rule_id,
        entity_type=entity_type,
        entity_id=f"google:{entity_type}:{entity}",
        rank=rank,
        action="Add as negative keyword",
        reason="Spent with no conversions",
        evidence=_evidence(provenance, name=entity),
        flags=[],
        data_status="live",
        held=False,
        held_reason=None,
        required_datasets=[],
        decision_subject={"type": "campaign", "id": 11} if rule_id == C0 else None,
    )


async def _run(session: AsyncSession, status: str = "succeeded") -> AnalysisRun:
    """Insert a custom run over the seed window with ``status``."""
    run = AnalysisRun(
        kind="custom",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        source_day_boundaries=[],
        trigger="manual",
        data_modes={},
        dataset_ids={},
        config_hash="0" * 64,
        default_days=28,
        trusted_min_days=14,
        status=status,
        started_at=NOW - timedelta(hours=2),
        finished_at=None if status == "running" else NOW - timedelta(hours=1),
    )
    session.add(run)
    await session.flush()
    return run


async def _feedback(
    session: AsyncSession, item: Recommendation, kind: str, *, until: datetime
) -> None:
    """Insert feedback on ``item``, copying its rule and entity as the API does."""
    session.add(
        RecommendationFeedback(
            recommendation_id=item.id,
            rule_id=item.rule_id,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            kind=kind,
            until=until,
            created_at=NOW - timedelta(days=1),
            created_by="admin@example.com",
        )
    )
    await session.flush()


async def _seed(session: AsyncSession) -> Seeded:
    """Commit a run whose ``block`` group spans three pages, with feedback on some items.

    The ``block`` items are inserted in shuffled order, so id order is not rank order, and two
    of them share :data:`TIED_RANK`. Creative and white-paper groups are dynamic, inserted in
    an order other than their lowest rank's. ``review``, ``scale`` and ``discrepancies`` apart
    from its one C0 item stay as the run left them.
    """
    google = SourceDataset(
        source="google",
        mode="live",
        state="active",
        role="primary",
        activated_at=NOW - timedelta(days=3),
    )
    session.add(google)
    await session.flush()
    provenance = [{"source": "google", "dataset_id": google.id, "mode": "live", "role": "primary"}]
    run = await _run(session)
    # A fixed permutation (97 is coprime with the prime 251), so ids follow no rank order.
    ranks = sorted([*range(1, BLOCK_ITEMS), TIED_RANK], key=lambda rank: (rank * 97) % 251)
    block = [
        _recommendation(
            run.id, provenance, section="keywords", group="block", rank=rank, entity=f"t{index}"
        )
        for index, rank in enumerate(ranks)
    ]
    expand = [
        _recommendation(
            run.id,
            provenance,
            section="keywords",
            group="expand",
            rank=rank,
            rule_id=K2,
            entity=f"e{rank}",
        )
        for rank in (1, 2, 3)
    ]
    c0 = _recommendation(
        run.id,
        provenance,
        section="campaigns",
        group="discrepancies",
        rank=1,
        rule_id=C0,
        entity="11",
    )
    dynamic = [
        _recommendation(
            run.id, provenance, section="creative", group=group, rank=rank, entity=f"c{rank}"
        )
        for group, rank in (
            ("campaign:20", 4),
            ("campaign:19", 2),
            ("campaign:20", 3),
            ("legacy", 1),
            ("campaign:19", 5),
        )
    ] + [
        _recommendation(
            run.id, provenance, section="white_papers", group=group, rank=rank, entity=f"w{rank}"
        )
        for group, rank in (("cohort:USD", 2), ("cohort:EUR", 1))
    ]
    session.add_all([*block, *expand, c0, *dynamic])
    await session.flush()
    by_rank: dict[int, list[Recommendation]] = {}
    for item in block:
        by_rank.setdefault(item.rank, []).append(item)
    hidden = [by_rank[rank][0] for rank in HIDDEN_BLOCK_RANKS]
    for item in hidden:
        await _feedback(session, item, "dismiss", until=NOW + timedelta(days=30))
    await _feedback(
        session, by_rank[EXPIRED_BLOCK_RANK][0], "dismiss", until=NOW - timedelta(days=1)
    )
    await _feedback(session, expand[1], "snooze", until=NOW + timedelta(days=7))
    await _feedback(session, c0, "dismiss", until=NOW + timedelta(days=30))
    await session.commit()
    order = sorted(block, key=lambda item: (item.rank, item.id))
    return Seeded(
        run_id=run.id,
        block_order=tuple(item.id for item in order),
        hidden_block=frozenset(item.id for item in hidden),
        expand_hidden=expand[1].id,
        c0_id=c0.id,
    )


@pytest.fixture
async def seeded(session: AsyncSession) -> Seeded:
    """The committed paging run on the emptied test database."""
    return await _seed(session)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def api(path: str) -> str:
    """Return ``path`` under the API prefix."""
    return f"{API_PREFIX}{path}"


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    """Assert ``response`` is exactly the AAP 0.5.3 error envelope with ``status`` and ``code``."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message"}, body
    assert body["error"]["code"] == code, body


def groups_of(page: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """The page's groups by ``(section, group)``."""
    return {
        (section["key"], group["key"]): group
        for section in page["sections"]
        for group in section["groups"]
    }


async def group_items(
    client: httpx.AsyncClient, run_id: int, section: str, group: str, **params: object
) -> dict[str, Any]:
    """``GET /recommendations/items`` for one group, asserting 200."""
    query = {"run_id": run_id, "section": section, "group": group, **params}
    response = await client.get(api("/recommendations/items"), params=query)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------------------------
# GET /recommendations: bounded groups (AAP 0.1.1, 0.5.3)
# ---------------------------------------------------------------------------------------------


async def test_the_page_carries_each_groups_first_items_and_counts_every_stored_one(
    authed_client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """A group carries its first ``GROUP_PAGE_SIZE`` items with its ``total`` and ``hidden``.

    The 250-item ``block`` group carries its first 100 in rank order (the tie by id), counts 250
    and the three items feedback in force hides, not the one whose feedback expired; the C0 item
    is never hidden. Every static group is present, empty or not, and the dynamic groups follow
    by their lowest rank, an unknown key last.
    """
    response = await authed_client.get(api("/recommendations"), params={"run_id": seeded.run_id})
    assert response.status_code == 200, response.text
    page = response.json()
    groups = groups_of(page)

    block = groups[("keywords", "block")]
    assert [item["id"] for item in block["items"]] == list(
        seeded.block_order[: schemas.GROUP_PAGE_SIZE]
    )
    assert (block["total"], block["hidden"]) == (BLOCK_ITEMS, len(HIDDEN_BLOCK_RANKS))
    served_hidden = {item["id"] for item in block["items"] if item["current"].get("hidden_by")}
    assert served_hidden == {
        item_id
        for item_id in seeded.hidden_block
        if item_id in seeded.block_order[: schemas.GROUP_PAGE_SIZE]
    }
    expand = groups[("keywords", "expand")]
    assert (len(expand["items"]), expand["total"], expand["hidden"]) == (3, 3, 1)
    assert [item["id"] for item in expand["items"] if item["current"].get("hidden_by")] == [
        seeded.expand_hidden
    ]
    assert groups[("keywords", "review")] == {
        "key": "review",
        "title": groups[("keywords", "review")]["title"],
        "items": [],
        "total": 0,
        "hidden": 0,
    }
    discrepancies = groups[("campaigns", "discrepancies")]
    assert (discrepancies["total"], discrepancies["hidden"]) == (1, 0)
    assert discrepancies["items"][0]["current"].get("hidden_by") is None

    sections = {section["key"]: section for section in page["sections"]}
    assert list(sections) == list(SECTION_ORDER)
    for key, static in GROUP_ORDER.items():
        keys = [group["key"] for group in sections[key]["groups"]]
        assert keys[: len(static)] == list(static), key
    assert [group["key"] for group in sections["creative"]["groups"]] == [
        "campaign:19",
        "campaign:20",
        "legacy",
    ]
    assert [group["key"] for group in sections["white_papers"]["groups"]] == [
        "cohort:EUR",
        "cohort:USD",
    ]
    for group in groups.values():
        assert len(group["items"]) <= min(group["total"], schemas.GROUP_PAGE_SIZE)
        assert len(group["items"]) == min(group["total"], schemas.GROUP_PAGE_SIZE)
        assert [item["rank"] for item in group["items"]] == sorted(
            item["rank"] for item in group["items"]
        )


# ---------------------------------------------------------------------------------------------
# GET /recommendations/items: the rest of a group (AAP 0.1.1, 0.5.3)
# ---------------------------------------------------------------------------------------------


async def test_paging_from_the_page_visits_every_item_of_a_group_once_in_rank_order(
    authed_client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """Pages from the page's item count on return every remaining item once, in rank order.

    Each slice repeats the group's ``total`` and ``hidden`` and the requested offset, and
    carries each item's ``current`` overlay exactly as the page would: the items feedback in
    force hides beyond the first page are hidden there too.
    """
    page = (
        await authed_client.get(api("/recommendations"), params={"run_id": seeded.run_id})
    ).json()
    first = groups_of(page)[("keywords", "block")]
    seen = [item["id"] for item in first["items"]]
    hidden = {item["id"] for item in first["items"] if item["current"].get("hidden_by")}
    offset = len(seen)
    while True:
        slice_ = await group_items(
            authed_client, seeded.run_id, "keywords", "block", offset=offset, limit=37
        )
        assert (slice_["run_id"], slice_["section"], slice_["group"]) == (
            seeded.run_id,
            "keywords",
            "block",
        )
        assert (slice_["total"], slice_["hidden"], slice_["offset"]) == (
            BLOCK_ITEMS,
            len(HIDDEN_BLOCK_RANKS),
            offset,
        )
        if not slice_["items"]:
            break
        assert len(slice_["items"]) <= 37
        seen.extend(item["id"] for item in slice_["items"])
        hidden.update(item["id"] for item in slice_["items"] if item["current"].get("hidden_by"))
        offset += len(slice_["items"])
    assert seen == list(seeded.block_order)
    assert len(set(seen)) == BLOCK_ITEMS
    assert hidden == seeded.hidden_block

    # The default limit is a full page; any offset reads the same order.
    default = await group_items(authed_client, seeded.run_id, "keywords", "block", offset=100)
    assert [item["id"] for item in default["items"]] == list(seeded.block_order[100:200])
    middle = await group_items(
        authed_client, seeded.run_id, "keywords", "block", offset=119, limit=3
    )
    assert [item["id"] for item in middle["items"]] == list(seeded.block_order[119:122])
    assert [item["rank"] for item in middle["items"]] == [TIED_RANK, TIED_RANK, TIED_RANK + 1]
    whole = await group_items(authed_client, seeded.run_id, "keywords", "expand")
    assert [item["id"] for item in whole["items"]] == [
        item["id"] for item in groups_of(page)[("keywords", "expand")]["items"]
    ]
    assert whole["items"] == groups_of(page)[("keywords", "expand")]["items"]


async def test_an_offset_past_the_group_or_an_unknown_group_returns_no_item(
    authed_client: httpx.AsyncClient, seeded: Seeded
) -> None:
    past = await group_items(
        authed_client, seeded.run_id, "keywords", "block", offset=schemas.BIGINT_MAX
    )
    assert (past["total"], past["offset"], past["items"]) == (
        BLOCK_ITEMS,
        schemas.BIGINT_MAX,
        [],
    )
    unknown = await group_items(authed_client, seeded.run_id, "keywords", "cohort:USD")
    assert (unknown["total"], unknown["hidden"], unknown["items"]) == (0, 0, [])
    empty = await group_items(authed_client, seeded.run_id, "keywords", "review")
    assert (empty["total"], empty["items"]) == (0, [])
    c0 = await group_items(authed_client, seeded.run_id, "campaigns", "discrepancies")
    assert (c0["total"], c0["hidden"]) == (1, 0)
    assert [item["id"] for item in c0["items"]] == [seeded.c0_id]
    assert c0["items"][0]["current"].get("hidden_by") is None


async def test_group_items_needs_a_session(api_client: httpx.AsyncClient, seeded: Seeded) -> None:
    response = await api_client.get(
        api("/recommendations/items"),
        params={"run_id": seeded.run_id, "section": "keywords", "group": "block"},
    )
    assert_error(response, 401, "unauthenticated")


async def test_group_items_answers_404_and_409_for_a_run_it_cannot_page(
    authed_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
) -> None:
    query = {"section": "keywords", "group": "block"}
    unknown = await authed_client.get(
        api("/recommendations/items"), params={**query, "run_id": seeded.run_id + 1000}
    )
    assert_error(unknown, 404, "not_found")
    for status in ("running", "failed"):
        run = await _run(session, status)
        await session.commit()
        response = await authed_client.get(
            api("/recommendations/items"), params={**query, "run_id": run.id}
        )
        assert_error(response, 409, "run_not_ready")
        assert status in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "query",
    [
        {"section": "keywords", "group": "block"},
        {"run_id": "0", "section": "keywords", "group": "block"},
        {"run_id": str(schemas.BIGINT_MAX + 1), "section": "keywords", "group": "block"},
        {"run_id": "seven", "section": "keywords", "group": "block"},
        {"run_id": "{run}", "group": "block"},
        {"run_id": "{run}", "section": "launch", "group": "block"},
        {"run_id": "{run}", "section": "keywords"},
        {"run_id": "{run}", "section": "keywords", "group": ""},
        {"run_id": "{run}", "section": "keywords", "group": "blo\x00ck"},
        {"run_id": "{run}", "section": "keywords", "group": "block", "offset": "-1"},
        {"run_id": "{run}", "section": "keywords", "group": "block", "limit": "0"},
        {"run_id": "{run}", "section": "keywords", "group": "block", "limit": "101"},
    ],
    ids=[
        "no-run",
        "run-zero",
        "run-beyond-bigint",
        "run-not-integer",
        "no-section",
        "unknown-section",
        "no-group",
        "empty-group",
        "nul-group",
        "negative-offset",
        "zero-limit",
        "limit-beyond-page",
    ],
)
async def test_group_items_refuses_a_parameter_outside_its_bounds(
    authed_client: httpx.AsyncClient, seeded: Seeded, query: dict[str, str]
) -> None:
    params = {key: value.format(run=seeded.run_id) for key, value in query.items()}
    response = await authed_client.get(api("/recommendations/items"), params=params)
    assert_error(response, 422, "validation_error")


async def test_group_items_reads_only_the_requested_slice(
    authed_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
) -> None:
    """The slice is the stored rows from the offset on: the same rows, frozen fields as stored."""
    slice_ = await group_items(
        authed_client, seeded.run_id, "keywords", "block", offset=200, limit=5
    )
    ids = [item["id"] for item in slice_["items"]]
    assert ids == list(seeded.block_order[200:205])
    stored = {
        row.id: row
        for row in await session.scalars(select(Recommendation).where(Recommendation.id.in_(ids)))
    }
    for item in slice_["items"]:
        row = stored[item["id"]]
        assert (item["rank"], item["rule_id"], item["action"], item["reason"]) == (
            row.rank,
            row.rule_id,
            row.action,
            row.reason,
        )
        assert item["entity"]["id"] == row.entity_id.split(":", 2)[2]
        assert item["current"]["historical"] is False
