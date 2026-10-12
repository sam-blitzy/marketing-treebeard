"""Database-enforced integrity of ``lead_match``, ``crm_contact`` and ``crm_association_fetch``.

The checks run against real PostgreSQL 18, because only the server proves what a constraint
accepts and rejects:

* ``fk_lead_match_contact_id_crm_contact`` is the composite (``contact_id``,
  ``crm_dataset_id``) -> ``crm_contact`` (``id``, ``dataset_id``), anchored by
  ``uq_crm_contact_dataset_id_id``: a match can only name a contact of its own CRM dataset,
  and a ``NULL`` contact is not checked.
* Purging a retired HubSpot dataset deletes only its own ``lead_match`` rows and contacts, and
  leaves the active dataset's matches untouched.
* The completed-state checks ``ck_lead_match_matched_has_contact``,
  ``ck_crm_association_fetch_ok_empty_shape`` and
  ``ck_crm_association_fetch_target_read_ok_complete`` reject contradictory states and leave
  every legitimate pending, failed or partial state alone.

The module creates a private schema in the database named by ``TEST_DATABASE_URL`` (falling
back to ``DATABASE_URL``), builds in it only the CRM tables under test and the tables their
foreign keys reach, pins every connection's ``search_path`` to it, and drops it with
``CASCADE`` at teardown, so ``public`` and every other schema stay untouched. Each test runs
in one transaction that is rolled back afterwards; expected violations run inside a savepoint.
An unreachable database fails the tests; it never skips them.
"""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from sqlalchemy import (
    Connection,
    Engine,
    Table,
    create_engine,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CheckConstraint, CreateSchema, DropSchema

from ads_engine.db.base import Base
from ads_engine.db.models import accounts, crm, ops

FOREIGN_KEY_VIOLATION = "23503"
CHECK_VIOLATION = "23514"

CONTACT_FK = "fk_lead_match_contact_id_crm_contact"
MATCHED_HAS_CONTACT = "ck_lead_match_matched_has_contact"
OK_EMPTY_SHAPE = "ck_crm_association_fetch_ok_empty_shape"
TARGET_READ_OK_COMPLETE = "ck_crm_association_fetch_target_read_ok_complete"

SUBMITTED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)

LEAD_MATCH: Table = Base.metadata.tables[crm.LeadMatch.__tablename__]
CRM_CONTACT: Table = Base.metadata.tables[crm.CrmContact.__tablename__]
ASSOCIATION_FETCH: Table = Base.metadata.tables[crm.CrmAssociationFetch.__tablename__]


@dataclass(frozen=True, slots=True)
class World:
    """Ids of the rows every test starts from.

    Attributes:
        linkedin_dataset: The active LinkedIn dataset holding the campaign and both leads.
        active_crm: The active primary HubSpot dataset.
        retired_crm: A retired primary HubSpot dataset, due for purge.
        lead_ids: Two leads of the LinkedIn dataset.
        active_contacts: Two contacts of ``active_crm``.
        retired_contacts: Two contacts of ``retired_crm``.
    """

    linkedin_dataset: int
    active_crm: int
    retired_crm: int
    lead_ids: tuple[int, int]
    active_contacts: tuple[int, int]
    retired_contacts: tuple[int, int]


# ---------------------------------------------------------------------------------------
# Database: a private schema per module, one rolled-back transaction per test
# ---------------------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the CRM integrity tests"
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


def _with_foreign_key_targets(*seeds: Table) -> list[Table]:
    """Return ``seeds`` together with every table their foreign keys reach, transitively."""
    found: dict[str, Table] = {}
    pending = list(seeds)
    while pending:
        table = pending.pop()
        if table.name in found:
            continue
        found[table.name] = table
        pending.extend(foreign_key.column.table for foreign_key in table.foreign_keys)
    return list(found.values())


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[Engine, str]]:
    """Create a private schema holding the CRM tables and their targets; drop it afterwards.

    Yields:
        An engine whose connections use only the private schema, and that schema's name.
    """
    url = _database_url()
    schema = f"crm_integrity_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    tables = _with_foreign_key_targets(LEAD_MATCH, ASSOCIATION_FETCH)
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection, tables=tables)
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield engine, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
def connection(database: tuple[Engine, str]) -> Iterator[Connection]:
    """Yield a connection inside a transaction that is rolled back after the test."""
    engine, _ = database
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _dataset(connection: Connection, source: str, state: str) -> int:
    """Insert a simulated primary ``source_dataset`` row and return its id."""
    statement = (
        insert(ops.SourceDataset)
        .values(source=source, mode="simulated", state=state, role="primary")
        .returning(ops.SourceDataset.id)
    )
    return int(connection.execute(statement).scalar_one())


def _contact(connection: Connection, dataset_id: int, hs_id: str) -> int:
    """Insert a HubSpot contact of ``dataset_id`` and return its id."""
    statement = (
        insert(crm.CrmContact)
        .values(source="hubspot", dataset_id=dataset_id, hs_id=hs_id)
        .returning(crm.CrmContact.id)
    )
    return int(connection.execute(statement).scalar_one())


def _lead(connection: Connection, dataset_id: int, campaign_id: int, external_id: str) -> int:
    """Insert a LinkedIn lead of ``campaign_id`` with no email and return its id."""
    statement = (
        insert(crm.Lead)
        .values(
            source="linkedin",
            dataset_id=dataset_id,
            external_id=external_id,
            submitted_at=SUBMITTED_AT,
            campaign_id=campaign_id,
            lead_form_urn="urn:li:leadGenForm:1",
            lead_form_version=1,
        )
        .returning(crm.Lead.id)
    )
    return int(connection.execute(statement).scalar_one())


@pytest.fixture
def world(connection: Connection) -> World:
    """Build one LinkedIn dataset with two leads, and an active and a retired CRM dataset."""
    linkedin = _dataset(connection, "linkedin", "active")
    active_crm = _dataset(connection, "hubspot", "active")
    retired_crm = _dataset(connection, "hubspot", "retired")
    account_id = connection.execute(
        insert(accounts.AdAccount)
        .values(
            source="linkedin",
            dataset_id=linkedin,
            external_id="500",
            name="Account",
            currency="USD",
        )
        .returning(accounts.AdAccount.id)
    ).scalar_one()
    campaign_id = connection.execute(
        insert(accounts.Campaign)
        .values(
            source="linkedin",
            dataset_id=linkedin,
            account_id=account_id,
            external_id="600",
            name="Campaign",
            status="active",
            raw_status="ACTIVE",
        )
        .returning(accounts.Campaign.id)
    ).scalar_one()
    return World(
        linkedin_dataset=linkedin,
        active_crm=active_crm,
        retired_crm=retired_crm,
        lead_ids=(
            _lead(connection, linkedin, campaign_id, "lead-1"),
            _lead(connection, linkedin, campaign_id, "lead-2"),
        ),
        active_contacts=(
            _contact(connection, active_crm, "101"),
            _contact(connection, active_crm, "102"),
        ),
        retired_contacts=(
            _contact(connection, retired_crm, "101"),
            _contact(connection, retired_crm, "102"),
        ),
    )


def _match(connection: Connection, lead_id: int, crm_dataset_id: int, **values: Any) -> None:
    """Insert one ``lead_match`` row for (``lead_id``, ``crm_dataset_id``)."""
    connection.execute(
        insert(crm.LeadMatch).values(lead_id=lead_id, crm_dataset_id=crm_dataset_id, **values)
    )


def _fetch(connection: Connection, dataset_id: int, **values: Any) -> None:
    """Insert one contact-to-meeting ``crm_association_fetch`` row of ``dataset_id``."""
    connection.execute(
        insert(crm.CrmAssociationFetch).values(
            source="hubspot",
            dataset_id=dataset_id,
            object_type="contact",
            object_id="101",
            to_type="meeting",
            fetched_at=FETCHED_AT,
            **values,
        )
    )


def _violation(error: IntegrityError) -> tuple[str | None, str | None]:
    """Return the SQLSTATE and the constraint name PostgreSQL reported for ``error``."""
    original = error.orig
    assert isinstance(original, psycopg.Error), original
    return original.sqlstate, original.diag.constraint_name


def _matches(connection: Connection) -> set[tuple[Any, ...]]:
    """Return every ``lead_match`` row as (lead, CRM dataset, status, contact, read, attempts)."""
    rows = connection.execute(
        select(
            crm.LeadMatch.lead_id,
            crm.LeadMatch.crm_dataset_id,
            crm.LeadMatch.status,
            crm.LeadMatch.contact_id,
            crm.LeadMatch.contact_read_status,
            crm.LeadMatch.attempts,
        )
    )
    return {tuple(row) for row in rows}


# ---------------------------------------------------------------------------------------
# (a) A match can only reference a contact of its own CRM dataset
# ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_a_match_rejects_a_contact_of_another_crm_dataset(
    connection: Connection, world: World
) -> None:
    """A match keyed to the active CRM dataset cannot name a retired dataset's contact."""
    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        _match(
            connection,
            world.lead_ids[0],
            world.active_crm,
            status="matched",
            contact_id=world.retired_contacts[0],
            contact_read_status="ok",
        )

    assert _violation(caught.value) == (FOREIGN_KEY_VIOLATION, CONTACT_FK)
    assert _matches(connection) == set()


@pytest.mark.db
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("contact", id="contact-moved-to-another-dataset"),
        pytest.param("dataset", id="match-moved-to-another-dataset"),
    ],
)
def test_an_update_cannot_cross_a_match_and_its_contact_between_datasets(
    connection: Connection, world: World, change: str
) -> None:
    """Re-pointing either side of a valid match at another CRM dataset is rejected."""
    _match(
        connection,
        world.lead_ids[0],
        world.active_crm,
        status="matched",
        contact_id=world.active_contacts[0],
        contact_read_status="ok",
    )
    values = (
        {"contact_id": world.retired_contacts[0]}
        if change == "contact"
        else {"crm_dataset_id": world.retired_crm}
    )

    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        connection.execute(update(crm.LeadMatch).values(**values))

    assert _violation(caught.value) == (FOREIGN_KEY_VIOLATION, CONTACT_FK)
    assert _matches(connection) == {
        (world.lead_ids[0], world.active_crm, "matched", world.active_contacts[0], "ok", 0)
    }


@pytest.mark.db
def test_a_match_accepts_a_contact_of_its_own_crm_dataset(
    connection: Connection, world: World
) -> None:
    """The same lead matches one contact per CRM dataset, each from that dataset."""
    _match(
        connection,
        world.lead_ids[0],
        world.active_crm,
        status="matched",
        contact_id=world.active_contacts[0],
        contact_read_status="ok",
    )
    _match(
        connection,
        world.lead_ids[0],
        world.retired_crm,
        status="matched",
        contact_id=world.retired_contacts[0],
        contact_read_status="error",
    )

    assert _matches(connection) == {
        (world.lead_ids[0], world.active_crm, "matched", world.active_contacts[0], "ok", 0),
        (world.lead_ids[0], world.retired_crm, "matched", world.retired_contacts[0], "error", 0),
    }


@pytest.mark.db
def test_a_match_without_a_contact_is_not_checked_against_any_dataset(
    connection: Connection, world: World
) -> None:
    """A ``NULL`` contact_id passes the composite key (MATCH SIMPLE) in every CRM dataset."""
    _match(connection, world.lead_ids[0], world.active_crm, status="pending")
    _match(connection, world.lead_ids[0], world.retired_crm, status="no_match", attempts=1)

    assert _matches(connection) == {
        (world.lead_ids[0], world.active_crm, "pending", None, None, 0),
        (world.lead_ids[0], world.retired_crm, "no_match", None, None, 1),
    }


@pytest.mark.db
def test_deleting_a_contact_deletes_only_the_matches_that_name_it(
    connection: Connection, world: World
) -> None:
    """The contact key cascades: removing a contact removes its matches and nothing else."""
    _match(
        connection,
        world.lead_ids[0],
        world.active_crm,
        status="matched",
        contact_id=world.active_contacts[0],
        contact_read_status="ok",
    )
    _match(
        connection,
        world.lead_ids[1],
        world.active_crm,
        status="matched",
        contact_id=world.active_contacts[1],
        contact_read_status="ok",
    )

    connection.execute(delete(crm.CrmContact).where(crm.CrmContact.id == world.active_contacts[0]))

    assert _matches(connection) == {
        (world.lead_ids[1], world.active_crm, "matched", world.active_contacts[1], "ok", 0)
    }


# ---------------------------------------------------------------------------------------
# (b) Purging a retired HubSpot dataset deletes only its own matching state
# ---------------------------------------------------------------------------------------


@pytest.mark.db
def test_purging_a_retired_crm_dataset_deletes_only_its_own_matches(
    connection: Connection, world: World
) -> None:
    """The one-statement purge of a retired HubSpot dataset leaves the active matches intact."""
    lead_1, lead_2 = world.lead_ids
    _match(
        connection,
        lead_1,
        world.active_crm,
        status="matched",
        attempts=2,
        contact_id=world.active_contacts[0],
        contact_read_status="ok",
    )
    _match(connection, lead_2, world.active_crm, status="no_match", attempts=3)
    _match(
        connection,
        lead_1,
        world.retired_crm,
        status="matched",
        attempts=1,
        contact_id=world.retired_contacts[0],
        contact_read_status="ok",
    )
    _match(
        connection,
        lead_2,
        world.retired_crm,
        status="matched",
        attempts=4,
        contact_id=world.retired_contacts[1],
        contact_read_status="error",
    )

    purged = connection.execute(
        text("DELETE FROM source_dataset WHERE id = :id"), {"id": world.retired_crm}
    )

    assert purged.rowcount == 1
    assert _matches(connection) == {
        (lead_1, world.active_crm, "matched", world.active_contacts[0], "ok", 2),
        (lead_2, world.active_crm, "no_match", None, None, 3),
    }
    contacts = connection.execute(
        select(crm.CrmContact.dataset_id, crm.CrmContact.id).order_by(crm.CrmContact.id)
    ).all()
    assert [tuple(row) for row in contacts] == [
        (world.active_crm, world.active_contacts[0]),
        (world.active_crm, world.active_contacts[1]),
    ]
    leads = connection.execute(select(crm.Lead.id).order_by(crm.Lead.id)).scalars().all()
    assert leads == list(world.lead_ids)


# ---------------------------------------------------------------------------------------
# (c) Completed-state checks
# ---------------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize(
    ("with_contact", "contact_read_status"),
    [
        pytest.param(False, "ok", id="matched-without-contact"),
        pytest.param(True, None, id="matched-without-read-status"),
        pytest.param(False, None, id="matched-without-either"),
    ],
)
def test_a_matched_row_needs_its_contact_and_read_status(
    connection: Connection, world: World, with_contact: bool, contact_read_status: str | None
) -> None:
    """``matched`` without a contact or without its read status is a check violation."""
    contact_id = world.active_contacts[0] if with_contact else None

    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        _match(
            connection,
            world.lead_ids[0],
            world.active_crm,
            status="matched",
            contact_id=contact_id,
            contact_read_status=contact_read_status,
        )

    assert _violation(caught.value) == (CHECK_VIOLATION, MATCHED_HAS_CONTACT)


@pytest.mark.db
def test_a_match_cannot_become_matched_without_its_contact(
    connection: Connection, world: World
) -> None:
    """Flipping a pending row to ``matched`` alone, without contact and read status, fails."""
    _match(connection, world.lead_ids[0], world.active_crm, status="pending")

    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        connection.execute(update(crm.LeadMatch).values(status="matched"))

    assert _violation(caught.value) == (CHECK_VIOLATION, MATCHED_HAS_CONTACT)


@pytest.mark.db
@pytest.mark.parametrize(
    ("status", "with_contact", "contact_read_status"),
    [
        pytest.param("pending", False, None, id="pending"),
        pytest.param("no_match", False, None, id="no_match"),
        pytest.param("no_email", False, None, id="no_email"),
        pytest.param("error", False, None, id="error"),
        pytest.param("matched", True, "ok", id="matched-read-ok"),
        pytest.param("matched", True, "error", id="matched-refresh-failed"),
    ],
)
def test_legitimate_match_states_are_accepted(
    connection: Connection,
    world: World,
    status: str,
    with_contact: bool,
    contact_read_status: str | None,
) -> None:
    """Unresolved rows need no contact; a matched row may carry a failed refresh."""
    contact_id = world.active_contacts[0] if with_contact else None

    _match(
        connection,
        world.lead_ids[0],
        world.active_crm,
        status=status,
        contact_id=contact_id,
        contact_read_status=contact_read_status,
    )

    assert _matches(connection) == {
        (world.lead_ids[0], world.active_crm, status, contact_id, contact_read_status, 0)
    }


@pytest.mark.db
@pytest.mark.parametrize(
    ("status", "target_read_status", "missing_target_ids", "constraint"),
    [
        pytest.param("ok_empty", "ok", [], OK_EMPTY_SHAPE, id="ok_empty-with-target-ok"),
        pytest.param("ok_empty", "partial", [], OK_EMPTY_SHAPE, id="ok_empty-with-target-partial"),
        pytest.param("ok_empty", "error", [], OK_EMPTY_SHAPE, id="ok_empty-with-target-error"),
        pytest.param("ok_empty", None, ["901"], OK_EMPTY_SHAPE, id="ok_empty-with-missing-ids"),
        pytest.param("ok", "ok", ["901"], TARGET_READ_OK_COMPLETE, id="ok-target-ok-missing"),
        pytest.param(
            "partial", "ok", ["901"], TARGET_READ_OK_COMPLETE, id="partial-target-ok-missing"
        ),
        pytest.param(
            "error", "ok", ["901", "902"], TARGET_READ_OK_COMPLETE, id="error-target-ok-missing"
        ),
    ],
)
def test_contradictory_fetch_states_are_rejected(
    connection: Connection,
    world: World,
    status: str,
    target_read_status: str | None,
    missing_target_ids: list[str],
    constraint: str,
) -> None:
    """An ``ok_empty`` with target reads or missing ids, or target reads ``ok`` with gaps, fail."""
    with pytest.raises(IntegrityError) as caught, connection.begin_nested():
        _fetch(
            connection,
            world.active_crm,
            status=status,
            target_read_status=target_read_status,
            missing_target_ids=missing_target_ids,
        )

    assert _violation(caught.value) == (CHECK_VIOLATION, constraint)


@pytest.mark.db
@pytest.mark.parametrize(
    ("status", "target_read_status", "missing_target_ids"),
    [
        pytest.param("ok_empty", None, [], id="ok_empty-genuine-zero"),
        pytest.param("ok", "ok", [], id="ok-every-target-read"),
        pytest.param("ok", "partial", ["901"], id="ok-target-partial-missing"),
        pytest.param("ok", "error", ["901", "902"], id="ok-target-error-missing"),
        pytest.param("ok", None, [], id="ok-targets-not-read"),
        pytest.param("partial", None, [], id="partial-targets-not-read"),
        pytest.param("partial", "partial", ["901"], id="partial-target-partial-missing"),
        pytest.param("error", None, [], id="error-targets-not-read"),
        pytest.param("error", None, ["901"], id="error-with-missing-ids"),
    ],
)
def test_legitimate_fetch_states_are_accepted(
    connection: Connection,
    world: World,
    status: str,
    target_read_status: str | None,
    missing_target_ids: list[str],
) -> None:
    """Known zeros, complete reads and every failed or partial outcome are stored as given."""
    _fetch(
        connection,
        world.active_crm,
        status=status,
        target_read_status=target_read_status,
        missing_target_ids=missing_target_ids,
    )

    stored = connection.execute(
        select(
            crm.CrmAssociationFetch.status,
            crm.CrmAssociationFetch.target_read_status,
            crm.CrmAssociationFetch.missing_target_ids,
        )
    ).one()
    assert tuple(stored) == (status, target_read_status, missing_target_ids)


@pytest.mark.db
def test_an_ok_empty_fetch_passes_with_the_server_default_missing_ids(
    connection: Connection, world: World
) -> None:
    """``missing_target_ids`` defaults to ``'{}'``, which satisfies the ``ok_empty`` shape."""
    _fetch(connection, world.active_crm, status="ok_empty")

    stored = connection.execute(select(crm.CrmAssociationFetch.missing_target_ids)).scalar_one()
    assert stored == []


# ---------------------------------------------------------------------------------------
# (d) Catalog: the constraint names and shapes the migration must reproduce
# ---------------------------------------------------------------------------------------

_CONSTRAINTS_SQL = text(
    "SELECT c.conname, c.contype, c.confdeltype, "
    "rel.relname AS table_name, ref.relname AS referred_table, "
    "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
    "ORDER BY k.ord) AS columns, "
    "ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute AS a ON a.attrelid = c.confrelid AND a.attnum = k.attnum "
    "ORDER BY k.ord) AS referred_columns "
    "FROM pg_constraint AS c "
    "JOIN pg_class AS rel ON rel.oid = c.conrelid "
    "JOIN pg_namespace AS n ON n.oid = c.connamespace "
    "LEFT JOIN pg_class AS ref ON ref.oid = c.confrelid "
    "WHERE n.nspname = :schema AND c.conname::text = ANY(:names)"
)


@pytest.mark.db
def test_the_catalog_holds_the_integrity_constraints(
    connection: Connection, database: tuple[Engine, str]
) -> None:
    """pg_constraint shows the composite contact key, its anchor and the three state checks."""
    _, schema = database
    names = [
        CONTACT_FK,
        "uq_crm_contact_dataset_id_id",
        MATCHED_HAS_CONTACT,
        OK_EMPTY_SHAPE,
        TARGET_READ_OK_COMPLETE,
    ]

    rows = connection.execute(_CONSTRAINTS_SQL, {"schema": schema, "names": names}).mappings()
    found = {row["conname"]: row for row in rows}

    assert sorted(found) == sorted(names)
    contact_fk = found[CONTACT_FK]
    assert contact_fk["contype"] == "f"
    assert contact_fk["table_name"] == "lead_match"
    assert contact_fk["columns"] == ["contact_id", "crm_dataset_id"]
    assert contact_fk["referred_table"] == "crm_contact"
    assert contact_fk["referred_columns"] == ["id", "dataset_id"]
    assert contact_fk["confdeltype"] == "c"
    anchor = found["uq_crm_contact_dataset_id_id"]
    assert (anchor["contype"], anchor["table_name"]) == ("u", "crm_contact")
    assert anchor["columns"] == ["dataset_id", "id"]
    for name, table in (
        (MATCHED_HAS_CONTACT, "lead_match"),
        (OK_EMPTY_SHAPE, "crm_association_fetch"),
        (TARGET_READ_OK_COMPLETE, "crm_association_fetch"),
    ):
        assert (found[name]["contype"], found[name]["table_name"]) == ("c", table)


@pytest.mark.db
def test_the_contact_key_keeps_its_supporting_index(
    connection: Connection, database: tuple[Engine, str]
) -> None:
    """``ix_lead_match_contact_id`` still leads with ``contact_id`` for the contact cascade."""
    _, schema = database

    definition: str = connection.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = :schema AND indexname = 'ix_lead_match_contact_id'"
        ),
        {"schema": schema},
    ).scalar_one()

    assert definition.endswith("lead_match USING btree (contact_id)")


def _check_sql(table: Table, name: str) -> str:
    """Return the SQL text of the ``CHECK`` constraint ``name`` declared on ``table``."""
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    pytest.fail(f"{table.name} declares no CHECK named {name}")


@pytest.mark.parametrize(
    ("table", "name", "module_sql", "expected"),
    [
        pytest.param(
            LEAD_MATCH,
            MATCHED_HAS_CONTACT,
            crm._MATCHED_HAS_CONTACT_SQL,
            "status <> 'matched' OR (contact_id IS NOT NULL AND contact_read_status IS NOT NULL)",
            id="matched_has_contact",
        ),
        pytest.param(
            ASSOCIATION_FETCH,
            OK_EMPTY_SHAPE,
            crm._OK_EMPTY_SHAPE_SQL,
            "status <> 'ok_empty' OR "
            "(target_read_status IS NULL AND cardinality(missing_target_ids) = 0)",
            id="ok_empty_shape",
        ),
        pytest.param(
            ASSOCIATION_FETCH,
            TARGET_READ_OK_COMPLETE,
            crm._TARGET_READ_OK_COMPLETE_SQL,
            "target_read_status <> 'ok' OR cardinality(missing_target_ids) = 0",
            id="target_read_ok_complete",
        ),
    ],
)
def test_the_state_checks_render_the_module_sql_verbatim(
    table: Table, name: str, module_sql: str, expected: str
) -> None:
    """Each completed-state CHECK carries exactly the module constant the migration copies."""
    assert _check_sql(table, name) == module_sql == expected
