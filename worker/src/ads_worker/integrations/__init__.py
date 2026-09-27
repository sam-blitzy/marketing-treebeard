"""The worker's only third-party-calling code (request Rule 1); Route Handlers never reach it, as
the web import-boundary lint rule blocks `**/integrations/**`. `http.py`: allowlisted transport,
retry policies, rate limiter, `StageError`/`ErrorCode`, `StageMetrics`. `fixtures.py`: synthetic
`FixtureSource` (zero-credential mode). `google_ads/`, `linkedin/`, `hubspot/`: each `client.py`,
`contracts.py`, `normalize.py`, plus `queries.py` (Google) and `auth.py` (LinkedIn). Every call is
read-only and no provider is ever written; Google is reached only via the existing n8n webhook."""
