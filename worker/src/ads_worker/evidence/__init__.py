"""Evidence records behind every displayed number (request Rule 2).

Each number the app renders is one stored ``ads.evidence`` row whose ``ads.evidence_source``
rows cite the rows it was computed from. Its provenance is computed from those cited sources,
never declared, so a value blending real and synthetic inputs is stored as NULL with
``mixed_provenance`` and test-data labels derive from it (AAP 0.6.4, Rule 14).

- ``builder.py``: ``EvidenceBuilder``, ``SourceRef``, ``DateWindow``, ``EvidenceRecord`` and
  ``EvidenceSpecError``.
- ``writer.py``: ``write_evidence(conn, builder)``, which bulk-inserts ``ads.evidence`` and then
  ``ads.evidence_source`` inside the caller's transaction (pipeline stage S5).

This marker deliberately imports and re-exports nothing: ``builder.py`` imports
``ads_worker.scoring.metric_specs`` while scoring modules import ``ads_worker.evidence.builder``,
so an eager import here would create an import cycle. Import the submodules directly.
"""
