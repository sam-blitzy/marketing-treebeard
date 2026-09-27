"""Read-only scope and contract audit (Gate 8(d); request Rules 4 and 5).

The ``scopes`` module compares the scopes granted to the LinkedIn and HubSpot credentials with
the exact read-only sets the request names, and reports Google Ads read-only access as an
accepted dependency that this build neither enforces nor verifies. It is used by
``python -m ads_worker audit-scopes`` and ``python -m ads_worker verify-run``.

Callers import ``ads_worker.audit.scopes`` directly. This marker imports and re-exports
nothing, so importing the package never loads httpx or the provider modules and cannot form a
cycle with ``ads_worker.integrations``.
"""
