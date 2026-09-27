"""HubSpot CRM v3 extractor for batch stage S3c; read-only (request Rule 5).

Authenticates with a legacy private app token (`HUBSPOT_PRIVATE_APP_TOKEN`) holding exactly
`crm.objects.contacts.read`, `crm.objects.deals.read` and `crm.schemas.contacts.read`. It never
writes to HubSpot, never calls the Search API, and never creates, updates or deletes a property.

Modules: `contracts` (pydantic models of the v3 responses), `client` (`HubSpotReader` Protocol,
`HubSpotClient`, `extract_hubspot`, `complete_associations`) and `normalize` (`normalize_hubspot`
producing a `HubSpotBatch`). HubSpot has announced that v1-v3 API paths and legacy private apps
become unsupported in September 2027 (risk R11); the owner decides the migration, recorded in
docs/integrations.md.
"""
