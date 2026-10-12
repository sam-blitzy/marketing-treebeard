"""Read-only connectors for the n8n Google Ads webhook, LinkedIn Marketing API and HubSpot CRM.

The package holds one HTTP client per external source:

* Google Ads through the existing n8n Cloud webhook, which relays one ``googleAds:search``
  v25 GAQL query per call. The product holds only the webhook URL, header name and shared
  secret; no Google OAuth credential or developer token exists here.
* The LinkedIn Marketing API, version ``202609``.
* The HubSpot CRM API, date version ``2026-09``.

Connectors perform HTTP only. They never touch the database and never decide what a failure
means for a source. Every failure is raised as exactly one of the ``ads_engine.connectors.errors``
types: ``AccessDenied``, ``TransientError`` (including its subclass ``CallBudgetExhausted``)
or ``QueryDefect``. ``http`` retries a transient error with backoff before raising it, and
``ads_engine.sync.mode`` alone decides what follows: a switch to simulated data, stale datasets
that keep their last good data, or a failed job. The same clients and parsers run unchanged in
simulated mode, because the simulator is injected as an httpx transport.

Submodules:
    errors: Error taxonomy and response classification.
    http: Shared httpx client factory, retry with backoff, and redacted request logging.
    google_n8n: Webhook connector, as ``client``, ``queries`` (Q-G1 to Q-G8 and Q-G2b),
        ``partition`` (keyset scans, id-range and date partitions) and ``parser``.
    linkedin: Marketing API connector, as ``auth`` (token provider), ``client`` and
        ``parser``.
    hubspot: CRM connector, as ``client``, ``search`` (partitioned CRM Search) and
        ``parser``.

The product only recommends. No connector ever writes to Google Ads, LinkedIn or HubSpot:
every call reads, and a change is applied by the user in the platform itself.

Importing this package has no side effects. It imports no submodule, third-party library or
settings; callers import the submodule they need explicitly.
"""
