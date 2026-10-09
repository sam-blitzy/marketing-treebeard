"""``GET /api/v1/healthz``: liveness and database check (AAP 0.5.3).

The route answers ``200 {"status": "ok"}`` when the API process is serving requests and
PostgreSQL completed one ``SELECT 1`` round trip within 3 s, and
``503 {"error": {"code": "database_unavailable", ...}}`` when it did not. The round trip
runs on a fresh connection outside the request pool
(:func:`~ads_engine.db.session.ping_database`) under the route's own
:func:`asyncio.timeout`, so a database that stopped answering, or a pool whose connections
are all stuck on it, gives a prompt 503 rather than a request that hangs. The root
``docker-compose.yml`` healthcheck calls ``http://127.0.0.1:8000/api/v1/healthz``, and
``web`` starts only once ``api`` reports healthy (AAP 0.11.2, first start).

It needs no session cookie: it is one of the two endpoints, with ``POST /auth/login``, that
AAP 0.5.3 exempts from authentication. It is a ``GET``, so the CSRF dependency that
``api/app.py`` attaches to every router passes it untouched. Monitoring is limited to
structured logs and this endpoint (AAP 0.7.2), so the module offers nothing else: no
metrics, no source probes, no version endpoint. It never calls LinkedIn, n8n or HubSpot and
imports nothing from ``connectors``, ``sync`` or ``simulation``.

Paths are relative to the ``/api/v1`` prefix that ``create_app`` adds::

    app.include_router(health.router, prefix="/api/v1", dependencies=[Depends(require_csrf)])
"""

import asyncio
from typing import Any, Final

from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError

from ads_engine.api import deps, schemas
from ads_engine.db.session import ping_database
from ads_engine.logging import get_logger

__all__ = ["healthz", "router"]

logger = get_logger(__name__)

router = APIRouter(tags=["health"])
"""The health router; ``create_app`` mounts it under ``/api/v1``."""

_PING_TIMEOUT_S: Final = 3.0
"""Seconds the database round trip may take before the check answers 503.

Short enough that a Compose or orchestrator probe sees the failure on its first attempt,
long enough that a healthy database on a busy host is never reported down.
"""

# The OpenAPI error map is local on purpose. The shared `schemas.ERROR_RESPONSES` documents
# 401 and 403 (session cookie, CSRF header), which this unauthenticated GET never returns,
# and lacks 503, the one error it does return. The body model stays the API-wide
# `schemas.ErrorResponse`, so the error shape is the same as every other route's.
_HEALTH_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    503: {
        "model": schemas.ErrorResponse,
        "description": "The database did not answer (code database_unavailable)",
    },
}


@router.get(
    "/healthz",
    response_model=schemas.Health,
    status_code=200,
    summary="Liveness and database check",
    responses=_HEALTH_RESPONSES,
)
async def healthz() -> schemas.Health:
    """Report that the API is up and PostgreSQL answers a round trip.

    Runs ``SELECT 1`` on every call through :func:`~ads_engine.db.session.ping_database`,
    on a fresh connection outside the request pool, inside an :func:`asyncio.timeout` of
    :data:`_PING_TIMEOUT_S`, with no caching and no rate limit, so the Compose healthcheck
    always reflects the live database. No authentication and no CSRF header are needed. The
    check is read-only.

    Returns:
        ``{"status": "ok"}``.

    Raises:
        ApiError: 503 ``database_unavailable`` when the round trip did not complete within
            :data:`_PING_TIMEOUT_S` (a paused or partitioned server), which the timeout
            reports as ``TimeoutError``, or failed: psycopg reports a refused or dropped
            connection either as a SQLAlchemy error or as an ``OSError``, depending on where
            it fails. Each is logged once as the WARNING ``health.database_unavailable``.
    """
    try:
        async with asyncio.timeout(_PING_TIMEOUT_S):
            await ping_database()
    except (TimeoutError, SQLAlchemyError, OSError) as exc:
        # Only the error class and the bound are logged: the exception text can name the
        # database URL, which carries the password.
        logger.warning(
            "health.database_unavailable",
            error_type=type(exc).__name__,
            timeout_s=_PING_TIMEOUT_S,
        )
        raise deps.ApiError(503, "database_unavailable", "Database unavailable") from exc
    return schemas.Health(status="ok")
