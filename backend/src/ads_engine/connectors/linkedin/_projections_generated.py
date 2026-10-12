"""Lead Sync ``fields`` projection generated from the LinkedIn contracts; do not edit by hand.

:mod:`ads_engine.connectors.linkedin.projections` writes this module from the
``request.fields`` that ``backend/tests/contracts/linkedin/lead_form_responses.json`` and
``lead_form_responses_batch.json`` both record; the contracts are the single authority for the
projection. After changing them, regenerate this file from ``backend/`` with::

    uv run --frozen python -m ads_engine.connectors.linkedin.projections

The same command with ``--check`` exits 1, and a unit test fails, while this file is stale.
"""

from typing import Final

LEAD_FIELDS: Final = (
    "ownerInfo,associatedEntityInfo,leadMetadataInfo,owner,leadType,versionedLeadGenFormUrn,id,"
    "submittedAt,testLead,formResponse,form:(hiddenFields,creationLocale,name,id,content)"
)
