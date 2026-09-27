"""Batch worker database layer for Postgres schema ``ads`` (Supabase Postgres 17).

``schema.py`` is the single schema source: Alembic autogenerates the revisions from it, and the
web's Kysely types are generated from the migrated database. ``engine.py`` builds the synchronous
SQLAlchemy 2.0 / psycopg 3 engine from ``WORKER_DATABASE_URL``, which the caller passes in.
``lock.py`` holds the session-level advisory lock that makes batch runs singletons.
"""
