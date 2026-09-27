"""Attribution engine: stage S4 (attribution) of the batch pipeline.

`pipeline/orchestrator.py` runs this stage after the three extract stages (S3a to S3c) and
before metrics and scoring (S5). It resolves HubSpot ad touches to `entity_catalog` entities and
writes the `attributed_outcome` rows that scoring reads, always within each integration's
effective provenance mode, so a real row is never matched against a synthetic one.

Modules:

- `params.py`: parses the `hsa_*`, `utm_grp` and `gclid` values of one HubSpot landing-URL
  history value into a `ParsedTouch`, driven by the `PARAM_MAP` constant table. It uses the
  standard library only, because `load/hubspot.py` imports it.
- `resolve.py`: translates bare tracking IDs into `entity_catalog` identities (LinkedIn URNs,
  Google resource names) in each integration's effective mode. It never guesses: an ambiguous
  or unmatched touch stays unresolved.
- `touches.py`: `is_true_transition` (imported by `load/hubspot.py`), `AttributionContext` and
  `build_touch_events`.
- `outcomes.py`: last-touch outcome attribution, the white-paper chain, the `dedup_key` rules,
  the Rule 9 ordering (an outcome never precedes its ad touch or its content touch),
  `bucket_date`, and the S4 entry point `run_attribution`.

This marker imports and re-exports nothing, so importing one module never loads the others;
callers import each module by its full path.

The package imports only `ads_worker.db`, `ads_worker.settings`, `ads_worker.log` and its own
modules, and never `integrations`, `load`, `evidence`, `scoring` or `pipeline`.
"""
