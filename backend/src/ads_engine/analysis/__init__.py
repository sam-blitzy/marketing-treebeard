"""Analysis: stored data in, five ranked sections of evidence-backed actions out.

The package turns the stored daily facts, entity snapshots, leads and CRM data of each
source's active dataset into the five ranked sections the product shows, in this fixed order:
Keywords to adjust, Campaigns to pause or launch, Budget changes, Copy/creative changes, and
White papers. Every item carries its evidence: the exact window with each source's native day
boundary, every number its reason cites, the baselines it was compared against, the sample
size against its minimum, any statistical test, the thresholds applied, and the datasets it
read with their frozen provenance.

Module map:
    context: rule contract (`evaluate(ctx: AnalysisContext) -> RuleResult`) and constants.
    thresholds: loader for config/*.yaml, `config_hash`, and the CRM stage-binding types.
    window: analysis windows as calendar-date ranges on each source's native day boundary.
    stats: Fisher's exact, two-proportion z-test, Holm, Wilson, power, Poisson rate test.
    text: search-term tokeniser, token Jaccard similarity, single-linkage clustering.
    metrics: SQL aggregation into `AnalysisContext`, budget days, utilization, efficiency, G1.
    attribution: white paper attribution chain and the CRM pipeline-veto population.
    keywords: rules K1-K3 (negative, expansion and review candidates).
    campaigns: rules C1, C1b, C2, C3a, C3b (pause, pipeline pause, scale or hold, launch).
    budget: rules B1-B4 (increase, do not increase, under-utilized, weekday/weekend).
    creative: rules CR1-CR3 (distinguishability, weak funnel stage, next single-variable test).
    white_papers: rules W1, W2 (double down, retire or rework) per currency cohort.
    discrepancy: rule C0 (platform state differs from the last decision), pinned first.
    precedence: G1 guard, P1-P8, feedback suppression, B3 allocation, deterministic ranking.
    runner: `run_analysis`, which builds the context, evaluates the rules and persists the run.
    lock: `analysis_lock` and `RunInProgress`, the one lock shared by API, worker and CLI.
    overlay: read-time current state (holds, source modes, freshness, feedback, historical).

I/O boundary:
    Only `runner`, `lock`, `overlay` and the SQL loaders in `metrics` touch the database.
    Only `thresholds` reads files, and only configuration, never the database:
    `Thresholds.load` parses config/thresholds.yaml, config/hubspot_mapping.yaml and
    config/content_assets.yaml, and `config_fingerprint` hashes their bytes.
    Every rule evaluator, and every other module, is pure: a rule receives an aggregated
    `AnalysisContext`, performs no I/O and returns a `RuleResult`, so it is deterministic and
    testable without a database.

This initialiser deliberately imports nothing. Importing `ads_engine.analysis` reads no
settings, creates no engine and loads neither SciPy nor SQLAlchemy; import the submodule
that is needed instead.
"""
