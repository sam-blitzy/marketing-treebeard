"""Worker process of the Ads Action Engine.

The worker owns all external I/O: it runs the connectors to the n8n Google Ads webhook,
the LinkedIn Marketing API and the HubSpot CRM API, drives the sync schedule, and
precomputes the analysis runs the API serves. Its entry point is `ads_engine.worker.main`,
run as `python -m ads_engine.worker.main`. The worker never serves HTTP and never writes
to any ad platform or CRM: the product only recommends.
"""
