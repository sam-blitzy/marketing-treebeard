"""``GET /config``: the read-only configuration behind ``/settings`` and the window picker.

AAP 0.5.3 (``GET /config``): read-only thresholds, including ``window.default_days``,
``window.trusted_min_days`` and ``window.presets``, the HubSpot mapping, the white paper
registry, the resolved CRM stage properties and ``config_hash``, as
:class:`~ads_engine.api.schemas.ConfigView`.

Why the web client needs it (AAP 0.5.5, 0.5.4.1):

* the window picker offers exactly ``window.presets`` and marks the preset equal to
  ``window.default_days`` as "default", so no window length is hard-coded in the client;
* the white-paper MQL column is headed with the bound stage label, which comes from the
  resolved bindings (``crm_bindings.mql.label``) once the portal has been verified;
* ``/settings`` shows the thresholds, the presets and default, the HubSpot mapping with its
  resolved stage properties, the registry and ``config_hash``.

Every value comes from the loaded ``config/*.yaml`` (:data:`~ads_engine.api.deps.ThresholdsDep`)
or from the database, never from a literal here, so a changed ``window.default_days`` is
reported as configured.

The resolved CRM bindings are a property of the HubSpot portal a dataset was read from. The
worker verifies them against the portal's own property definitions and persists them on the
HubSpot dataset as ``source_dataset.crm_bindings``; this route only reads them back. **The API
never calls HubSpot**: this module imports nothing from ``connectors``, ``sync`` or
``simulation``, and makes no outbound request.

Example:
    ``GET /api/v1/config`` with a session cookie returns, abridged::

        {
          "thresholds": {"window": {"default_days": 28, "presets": [14, 28, 56, 90], ...}, ...},
          "window": {"default_days": 28, "trusted_min_days": 14, "presets": [14, 28, 56, 90],
                     "max_days": 180, "sustained_min_days": 7},
          "hubspot_mapping": {"mql": {...}, "ql": {...}, "booking_sources": ["MEETINGS_PUBLIC"],
                              "meeting_outcome_exclusions": ["CANCELED"]},
          "crm_bindings": {"mql": {"available": true, "property": "hs_v2_date_entered_...", ...},
                           ...},
          "companion_crm_bindings": null,
          "content_assets": [{"slug": "b2b-pipeline-report-2026", ...}, ...],
          "config_hash": "3f1c..."
        }
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from ads_engine.analysis.thresholds import CrmBindings
from ads_engine.api import schemas
from ads_engine.api.deps import SessionDep, ThresholdsDep, UserDep
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.repositories import active_datasets

__all__ = ["get_config", "router"]

router = APIRouter(tags=["config"])


def _bindings_view(dataset: SourceDataset | None) -> schemas.CrmBindingsView | None:
    """Return a HubSpot dataset's stored CRM bindings as their typed view, or ``None``.

    The stored document is decoded strictly with :meth:`CrmBindings.from_json` and rendered
    with :meth:`~ads_engine.api.schemas.CrmBindingsView.from_bindings`. The response therefore
    carries every key of :meth:`CrmBindings.to_json`, with ``verified_at`` as the same instant
    in UTC, and a corrupt row fails loudly instead of passing an unvalidated document to the
    client.

    Args:
        dataset: The HubSpot dataset whose bindings to show; ``None`` when there is none.

    Returns:
        The bindings view, or ``None`` when there is no dataset or its bindings have not been
        verified yet, which the UI shows as "not yet verified".

    Raises:
        ads_engine.analysis.thresholds.ConfigError: When the stored document is not a valid
            ``crm_bindings`` document; ``api/app.py`` renders it as 500 ``config_error``.
    """
    if dataset is None:
        return None
    stored: Mapping[str, Any] | None = dataset.crm_bindings
    if stored is None:
        return None
    return schemas.CrmBindingsView.from_bindings(CrmBindings.from_json(stored))


@router.get(
    "/config",
    response_model=schemas.ConfigView,
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def get_config(
    user: UserDep, session: SessionDep, thresholds: ThresholdsDep
) -> schemas.ConfigView:
    """Return the read-only configuration: thresholds, window, mapping, bindings, registry.

    AAP 0.5.3 ``GET /config``. The response carries:

    * ``thresholds``: every key of ``config/thresholds.yaml`` by namespace, with ``crm`` also
      carrying the mapping's ``booking_sources`` and ``meeting_outcome_exclusions``;
    * ``window``: ``window.default_days``, ``window.trusted_min_days``, ``window.presets``,
      ``window.max_days`` and ``window.sustained_min_days``, each read from the loaded
      configuration;
    * ``hubspot_mapping``: the MQL and Qualified Lead bindings and the meeting rules as
      configured in ``config/hubspot_mapping.yaml``;
    * ``crm_bindings``: the bindings resolved against the portal of HubSpot's active primary
      dataset, or ``None`` until the worker has verified them;
    * ``companion_crm_bindings``: the bindings of the active companion CRM dataset (the
      simulated portal read while LinkedIn is simulated), or ``None`` when there is none;
    * ``content_assets``: the white paper registry, in file order;
    * ``config_hash``: the hash of the three configuration files, as stored on every run.

    The bindings are read from ``source_dataset.crm_bindings``, where the worker stores them;
    this route never calls HubSpot. It is read-only and never commits.

    Args:
        user: The signed-in user; the dependency enforces the session cookie.
        session: The request's database session.
        thresholds: The loaded ``config/*.yaml``.

    Returns:
        The configuration view.

    Raises:
        ApiError: 401 ``unauthenticated`` without a valid session (from the dependency).
        ads_engine.analysis.thresholds.ConfigError: When a configuration file or a stored
            ``crm_bindings`` document is invalid; rendered as 500 ``config_error``.
        sqlalchemy.exc.MultipleResultsFound: When more than one companion CRM dataset is
            active, which the dataset lifecycle never produces.
    """
    primary = (await active_datasets(session)).get("hubspot")
    # A companion shares its LinkedIn parent's state and LinkedIn has at most one active
    # primary dataset, so at most one companion is active; a second one is a broken
    # lifecycle invariant and fails the request rather than showing an arbitrary portal.
    companion = (
        await session.execute(
            select(SourceDataset).where(
                SourceDataset.source == "hubspot",
                SourceDataset.role == "companion",
                SourceDataset.state == "active",
            )
        )
    ).scalar_one_or_none()
    return schemas.ConfigView(
        thresholds=thresholds.as_view(),
        window=schemas.WindowConfig.from_thresholds(thresholds),
        hubspot_mapping=schemas.HubSpotMappingView.from_mapping(thresholds.mapping),
        crm_bindings=_bindings_view(primary),
        companion_crm_bindings=_bindings_view(companion),
        content_assets=[
            schemas.ContentAssetView.from_asset(asset) for asset in thresholds.content_assets
        ],
        config_hash=thresholds.config_hash,
    )
