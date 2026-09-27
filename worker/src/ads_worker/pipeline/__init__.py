"""Batch orchestration for stages S0-S7 of the daily run (AAP 0.3.4).

Modules:

- ``orchestrator.py`` — ``run_batch(settings, transports=None, *, clock=None) -> RunResult``:
  advisory lock, stages S0-S7, per-integration stage isolation, exit codes 0/1/3.
- ``sources.py`` — S1 source selection (``select_sources``), effective modes
  (``effective_modes``) and S2 refresh plans (``build_refresh_plans``).
- ``runs.py`` — ``run`` row bootstrap and finalisation, ``run_integration`` rows, run-status
  evidence and scoring-output bulk writes.
- ``publish.py`` — S6 atomic publish through ``app_state.published_run_id``.
- ``retention.py`` — S7 pruning of derived rows beyond ``ADS_RETENTION_RUNS``.

The worker reads ``white_papers`` and ``decision_log`` but never writes them: they are the
web app's two user-facing write paths.

This package is deliberately inert (no imports, no re-exports): the orchestrator imports every
other subpackage, so an eager import here would make ``import ads_worker.pipeline.publish`` load
the whole worker and risk import cycles.
"""
