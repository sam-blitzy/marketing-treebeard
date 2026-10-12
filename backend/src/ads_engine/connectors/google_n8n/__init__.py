"""Google Ads connector: the n8n Cloud webhook relay to ``googleAds:search`` v25.

This package is the product's only access path to Google Ads. Every call is one ``POST`` to the
existing n8n Cloud webhook, authenticated by the shared-secret header named in
``N8N_WEBHOOK_SECRET_HEADER``, with exactly this JSON body and no other keys::

    {"customer_id": "<10 digits, no dashes>", "query": "<GAQL>", "page_token": null}

``page_token`` is ``null`` on a first page and otherwise carries a returned ``nextPageToken``
back at once. Each call relays exactly one GAQL query, which n8n's already-authenticated Google Ads
node runs as ``googleAds:search`` v25 and answers synchronously with
``{results[], fieldMask, queryResourceConsumption}``. ``searchStream`` is never used. A page
holds at most 10,000 rows and the specified response carries no page token, so completeness
comes from keyset scans and partitioning, never from an id list read off a capped response.

No Google OAuth flow, client id, client secret, refresh token or developer token exists in the
product: they stay in n8n's own credential store. The product holds only the webhook URL, the
header name and the secret. The connector is read-only and never writes to Google Ads.

Submodules:
    queries: ``QUERIES``, one ``QueryTemplate`` per id for the GAQL queries Q-G1 to Q-G8 and
        Q-G2b, including the keyset forms of Q-G2, Q-G4 and Q-G7. Standard library only.
    parser: ``PARSERS``, keyed by row surface, turning camelCase REST ``GoogleAdsRow`` objects
        (int64 values as strings) into typed rows: micros to exact amounts, int64 strings to
        integers, conversions kept as decimals.
    client: ``GoogleAdsWebhookClient``, one webhook call per GAQL query, following a page token
        when one is present; the ``CallBudget`` protocol that meters every call against the
        product's own cap, and ``UNMETERED`` for simulated runs and tests.
    partition: ``PartitionedFetcher``, which binds one webhook client, and so its call budget,
        at construction; its ``partitioned_fetch(query_id, scope)`` reports each fetch as
        complete or incomplete through keyset scans and id-range and date partitioning. Also
        the safe-absence enumerations that let ``sync`` infer a removed or missing entity only
        from complete reads.

Dependencies run one way: ``queries`` <- ``parser`` <- ``client`` <- ``partition``. Nothing here
imports ``sync``, ``simulation``, ``db``, ``analysis`` or ``api``; ``sync`` injects the call
budget. Errors leave as ``AccessDenied``, ``TransientError`` (including ``CallBudgetExhausted``)
or ``QueryDefect`` only, and ``ads_engine.sync.mode`` decides what follows.

Consumers import the submodule they need explicitly, for example::

    from ads_engine.connectors.google_n8n.client import GoogleAdsWebhookClient

This module has no imports and no code, so importing the package loads neither ``httpx`` nor
settings nor any submodule.
"""
