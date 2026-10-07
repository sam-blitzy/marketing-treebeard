"""Read-only LinkedIn Marketing API connector.

Every request targets API version ``202609`` (``Linkedin-Version: 202609`` with
``X-Restli-Protocol-Version: 2.0.0``) under the base URL ``https://api.linkedin.com/rest/``.
The connector only reads: it never writes to LinkedIn, and the product has no OAuth consent
UI. Tokens are bootstrapped once with the Developer Portal token generator.

Submodules:
    auth: In-memory access-token provider. It exchanges the server-side refresh token for an
        access token, or uses a static access token, and never persists either.
    client: Rest.li 2.0 requests for ad accounts, campaign groups, campaigns, creatives,
        posts, daily creative analytics, demographic segment statistics, lead forms, lead
        responses and the transient batch re-fetch of lead responses by id.
    parser: Typed rows from LinkedIn payloads, versioned lead-form URNs split into the
        canonical form URN plus version, and lead email hashing.

Personal data: a lead email exists only in memory while it is matched to a HubSpot contact.
It is persisted only as the SHA-256 hash of the normalised (trimmed, lowercased) address.

This package marker imports nothing, so importing it never loads httpx or the settings.
Consumers import the submodule they need explicitly, for example::

    from ads_engine.connectors.linkedin.client import LinkedInClient
"""
