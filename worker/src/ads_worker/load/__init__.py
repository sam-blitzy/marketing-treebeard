"""Loaders: the load half of batch stage S3 (extract, validate, normalize, load).

Each loader writes one integration's normalized provider batch into schema ``ads`` inside a
single transaction owned by the caller, so a failure rolls back that integration alone and
leaves its prior rows and the other integrations untouched (request Rule 13). Every row is
tagged with ``provenance`` (``real`` or ``synthetic``), which is part of every unique key, so a
synthetic row can never overwrite a real one or the reverse (request Rule 14). Facts and HubSpot
records follow the versioned reconcile policy: a key returned with changed values closes its
current version (``valid_to_seq = run.seq``) and inserts a new one, an unchanged key is left
alone, and a current key the provider no longer returns within the refreshed scope is closed.

Modules:

- ``catalog``: ``LoadResult`` and ``LoadIntegrityError``, entity upserts into
  ``entity_catalog``, insert-only ``budget_snapshot`` captures, and ``entity_member`` rows for
  keyword clusters and targeting segments.
- ``facts``: ``load_google(...)`` and ``load_linkedin(...)``, which reconcile ``fact_daily``,
  ``google_click`` and ``google_click_day``.
- ``hubspot``: ``load_hubspot(...)``, which reconciles ``hs_contact``, ``hs_contact_event``,
  ``hs_deal``, ``hs_deal_contact``, ``hs_meeting`` and ``hs_meeting_contact``, and writes
  ``run_white_paper.property_check``.

The worker never writes ``white_papers`` or ``decision_log``, the two user-owned tables, and no
raw contact URL is stored: only parsed tracking parameters reach ``hs_contact_event.hsa``.

This package deliberately imports nothing, so importing ``ads_worker.load.facts`` does not load
``ads_worker.load.hubspot`` and, through it, ``ads_worker.attribution``. Import the submodule
that is needed.
"""
