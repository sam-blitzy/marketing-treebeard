"""Read-only LinkedIn Marketing API extractor, stage S3b (Linkedin-Version 202609, Rest.li 2.0.0).

Modules: ``contracts`` (pydantic REST shapes), ``auth`` (in-memory access-token mint, IR6),
``client`` (``LinkedInReader`` Protocol, ``LinkedInClient``, ``extract_linkedin``), ``normalize``
(``normalize_linkedin`` -> ``LinkedInBatch``). Request §3 naming: API campaign group = UI
"Campaign", API campaign = UI "Ad Set" (this build's ``campaign_id``), API creative = UI "Ad".
"""
