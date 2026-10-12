"""The ``/api/v1`` REST routers of the Ads Action Engine.

One module per route group. Paths below are relative to ``/api/v1``:

- ``auth``: ``POST /auth/login``, ``POST /auth/logout``, ``GET /auth/me``.
- ``runs``: ``GET /runs/latest``, ``GET /runs/{id}``, ``GET /runs``, ``POST /runs``.
- ``recommendations``: ``GET /recommendations``, ``POST /recommendations/{id}/feedback``.
- ``decisions``: ``GET /decisions``, ``POST /decisions``.
- ``discrepancies``: ``GET /discrepancies``, ``POST /discrepancies/{id}/resolve``.
- ``sources``: ``GET /sources``, ``POST /sources/{source}/sync``.
- ``config``: ``GET /config``.
- ``health``: ``GET /healthz``.

Each module exposes ``router``, and ``ads_engine.api.app.create_app`` includes them with
prefix ``/api/v1`` and the CSRF dependency. This package deliberately imports nothing:
``create_app`` imports each ``ads_engine.api.routes.<module>`` explicitly, so importing the
package has no side effects and cannot create an import cycle.
"""
