"""Worker I/O core: source modes, connector syncs, snapshots, lead matching and scheduling.

This package owns all external I/O of the worker. It decides per source whether LinkedIn,
Google Ads (through the n8n webhook) and HubSpot run live or simulated, runs the connectors,
loads idempotent daily facts and append-only entity snapshots into each source's dataset,
matches LinkedIn leads to HubSpot contacts without storing emails, records freshness and
completeness per source dataset, caps the product's own Google webhook calls, and schedules
every sync job and analysis request.

Submodules:
    freshness: ``record_freshness`` and ``FreshnessOutcome``, the ``dataset_freshness``
        write per source dataset (last attempt and success, coverage, completeness, reason).
    budget_guard: ``GoogleCallBudget``, the cap on n8n webhook calls per sliding 24 h
        (``GOOGLE_DAILY_CALL_BUDGET``) that counts every call, partition and next page.
    datasets: ``DatasetManager`` and ``Readiness``: building and backfilling datasets,
        companion CRM datasets, the staged-matching readiness test, atomic activation,
        retirement and the cascade purge of retired datasets.
    lead_match: Durable lead-to-contact matching per CRM dataset: due-row claiming,
        transient email re-fetch, SHA-256 email hashes only, and the retry schedule.
    snapshots: ``write_snapshots``, ``ensure_baseline`` and ``detect_discrepancies``:
        append-only entity snapshots, baseline decisions on the first entity sync of each
        new dataset, and the discrepancy check that follows every entity sync.
    mode: ``SourceModeController`` and ``bootstrap_sources``: the live or simulated mode
        per source, the consequence of each classified connector error, and the recovery
        probe of a source's full capability set.
    jobs: ``EntitySyncJob``, ``MetricsSyncJob``, ``SegmentSyncJob``, ``LeadSyncJob``,
        ``LeadMatchJob``, ``CrmSyncJob`` and ``run_source_jobs``: the per-source sync jobs
        and the runner that executes them in order.
    scheduler: ``Scheduler`` and ``SCHEDULER_LOCK_KEY``: the 30 s loop held under the
        scheduler advisory lock that runs due jobs, polls ``sync_request`` rows and
        coalesces ``analysis_request`` rows.

This package never imports ``ads_engine.api`` or ``ads_engine.worker``: the worker entry
point depends on it, never the reverse. Importing it has no side effects: it reads no
settings, creates no engine or HTTP client and builds no simulated world, so import the
submodules explicitly, for example ``from ads_engine.sync.scheduler import Scheduler``.
"""
