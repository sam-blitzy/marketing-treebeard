"""Read-only HubSpot CRM connector.

Every call uses its surface's dated ``2026-09`` path: objects batch read, CRM Search,
associations batch read and properties. No numbered ``v3`` or ``v4`` path is ever used.

Submodules:
    client: ``HubSpotClient`` and its ``RateLimiter``; property verification, contacts by
        email or id, associations, meetings and deals.
    parser: typed ``ContactRow``, ``MeetingRow`` and ``DealRow`` and the response decoders.
    search: ``search_partitioned``, the partitioned CRM Search for deals and Google-touch
        contacts.

The product is recommend-only, so this connector only reads: it never writes to HubSpot
and never touches the database. It raises only ``AccessDenied``, ``TransientError`` or
``QueryDefect`` from ``ads_engine.connectors.errors``; ``ads_engine.sync.mode`` decides
the consequence.

This package imports none of its submodules. Import them explicitly, for example
``from ads_engine.connectors.hubspot.client import HubSpotClient``.
"""
