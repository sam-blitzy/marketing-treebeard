"""REST API of the Ads Action Engine, mounted at ``/api/v1``.

The application is built by the factory ``ads_engine.api.app:create_app`` and served with
``uvicorn ads_engine.api.app:create_app --factory``.

Submodules:
    app: Application factory, middleware and error handlers.
    deps: Request dependencies and ``ApiError``.
    security: argon2id password hashing, session helpers, the login rate limiter and the
        CSRF header constants.
    schemas: Pydantic request and response contract, mirrored by ``web/src/api/types.ts``.
    routes: One router per resource.

The API reads stored results only. It never calls LinkedIn, n8n or HubSpot, and never
imports ``ads_engine.connectors``, ``ads_engine.sync`` or ``ads_engine.simulation``; all
external I/O belongs to the worker process.

Importing this package has no side effects: it reads no settings, creates no database
engine and configures no logging.
"""
