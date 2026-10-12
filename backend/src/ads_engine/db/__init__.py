"""Ads Action Engine persistence on PostgreSQL 18 with async SQLAlchemy 2.1 and psycopg 3.

Submodules:
    base: Declarative ``Base``, the constraint naming convention and the shared column
        types (exact ``numeric`` money and conversions, ``timestamptz`` instants).
    session: Async engine, the transactional session scope, and the dedicated connection
        that holds session-level advisory locks (scheduler and ``analysis``).
    repositories: Idempotent dataset-scoped upserts keyed on natural keys, append-only
        snapshot writes, shared reads of each source's active dataset, analysis-request
        enqueueing, and synchronisation of the content-asset registry.
    models: ORM tables of the schema created by the initial Alembic migration.

Importing this package has no side effects: it creates no engine and loads no settings,
so import the submodules explicitly, for example ``from ads_engine.db.session import ...``.
"""
