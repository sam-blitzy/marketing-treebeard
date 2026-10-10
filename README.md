# Ads Action Engine

Repository: `marketing-treebeard`

The Ads Action Engine is an internal tool that continuously syncs the company's LinkedIn Ads
accounts, its Google Ads account (through the existing n8n Cloud webhook) and its HubSpot CRM,
and turns that data into ranked, concrete actions in five fixed sections, in this order:
**Keywords to adjust**, **Campaigns to pause or launch**, **Budget changes**,
**Copy/creative changes** and **White papers**. It is not a dashboard to interpret: every item
reads "do this because of this", leads with the action, and carries the numbers and the exact
date window that justify it. The product is **recommend-only**. It never writes to LinkedIn,
Google Ads or HubSpot: you apply each change in the platform and record the decision under
Decisions, and the next entity snapshot either confirms the change or raises a discrepancy,
which is shown before every other recommendation.

## Contents

- [How it works](#how-it-works)
- [Data sources](#data-sources)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quick start (simulated data, no credentials)](#quick-start-simulated-data-no-credentials)
- [Going live](#going-live)
- [Environment variables](#environment-variables)
- [Make targets](#make-targets)
- [Configuration files](#configuration-files)
- [Security](#security)
- [Development and CI](#development-and-ci)
- [Design system](#design-system)
- [Out of scope](#out-of-scope)
- [Documentation](#documentation)

## How it works

- **Modular monolith.** One Python 3.14 package, `ads_engine` (`backend/src/ads_engine/`), runs
  as two processes built from one image: `api` (FastAPI under uvicorn) and `worker`. A React 19
  single-page app is served by nginx, and PostgreSQL 18 is the only state store.
- **The worker owns all external I/O.** It runs the scheduled syncs, writes daily facts and dated
  entity snapshots, checks every entity sync against the decision log for discrepancies, and
  precomputes the default-window analysis after every data or decision change.
- **The API never calls LinkedIn, n8n or HubSpot.** It serves stored analysis runs with a
  current-state overlay (open discrepancies, source modes, data freshness, feedback) and computes
  custom-window runs from stored data only.
- **The SPA talks only to `/api/v1`**, through nginx.

Components, schedule, datasets and fallback: [docs/architecture.md](docs/architecture.md).

## Data sources

- **Google Ads**, only through the existing n8n Cloud webhook. Each call is
  `POST {N8N_GOOGLE_ADS_WEBHOOK_URL}` with the shared-secret header, carrying one GAQL query that
  n8n runs as `googleAds:search` on API v25, never `searchStream`. The JSON body is:

  ```json
  {"customer_id": "<10 digits, no dashes>", "query": "<GAQL>", "page_token": null}
  ```

  `page_token` is `null` on a query's first call; when a response returns a `nextPageToken`, the
  next call sends that string back as `page_token`. No Google client ID, client secret,
  refresh token or developer token exists in the product; they stay in n8n's credential store.
  The Google Ads quota is shared with every other n8n workflow on that credential, so the product
  caps its own webhook calls at `GOOGLE_DAILY_CALL_BUDGET` (default 1000) per sliding 24 h.
  Details: [docs/integrations/google-ads-n8n.md](docs/integrations/google-ads-n8n.md).
- **LinkedIn Marketing API**, version `202609`, with the read-only scopes `r_ads`,
  `r_ads_reporting` and `r_marketing_leadgen_automation` (Lead Sync). Tokens are bootstrapped
  once with the Developer Portal token generator
  ([runbook](docs/runbook.md#linkedin-token-bootstrap)); the product has no consent UI.
  Details: [docs/integrations/linkedin.md](docs/integrations/linkedin.md).
- **HubSpot CRM**, on the `2026-09` API paths, with a Service Key or private-app token that holds
  read scopes for contacts, deals and meetings. MQL ("First meeting booked") and Qualified Lead
  are bound to the portal's own lifecycle stages in `config/hubspot_mapping.yaml`.
  Details: [docs/integrations/hubspot.md](docs/integrations/hubspot.md).

A source without working access runs on simulated data in that source's live wire format,
labelled "Simulated" everywhere ([Quick start](#quick-start-simulated-data-no-credentials)).

## Repository layout

```text
.
├── backend/                  Python package ads_engine (api, worker, ads-engine CLI), Alembic
│                             migrations, tests, pyproject.toml, uv.lock, Dockerfile
├── web/                      React SPA (TypeScript, Vite), design-system layer, Vitest and
│                             Playwright tests, package-lock.json, Dockerfile, nginx.conf
├── config/                   thresholds.yaml, content_assets.yaml, hubspot_mapping.yaml
├── docs/                     Architecture, recommendation rules, integrations, runbook
├── .github/workflows/ci.yml  CI: lint, type check, tests, build, end-to-end, secret scan
├── docker-compose.yml        Local stack: db, migrate, api, worker, web (127.0.0.1:8080)
├── Makefile                  Tool checks, first start, stack control, tests, lint, secret scan
├── .env.example              Every environment variable: safe defaults, empty secrets
└── .gitleaks.toml            Secret-scan rules and allowlists
```

## Prerequisites

| Tool | Version | Needed for |
| --- | --- | --- |
| Docker Engine | 29 or newer | Every container |
| Docker Compose plugin | 5.5.1 or newer, below 6 | The stack in `docker-compose.yml` |
| GNU make | 4.0 or newer, with bash | Every `make` target |
| `curl` or `wget` | Any current release | The health poll of `make up` and `make e2e` |
| uv | Exactly 0.12.21, with Python 3.14 | Backend tests and lint on the host only |
| Node.js | 24.21.0 or newer, with npm | Web tests, lint and end-to-end tests on the host only |

The running product needs only Docker, Compose and make: the images bring their own Python
(`python:3.14.8-slim`), Node (`node:24.21.0-slim`, build stage) and nginx (`nginx:1.30.5`).
`make check-tools` verifies the Docker versions and, on failure, prints the version it found and
the required range:

```bash
make check-tools
```

## Quick start (simulated data, no credentials)

1. Write `.env`:

   ```bash
   make init ADMIN_PASSWORD='<choose-a-password>'
   ```

   `make init` runs `make check-tools`, refuses to overwrite an existing `.env`, copies
   `.env.example` to `.env` (mode 600), writes a random `SESSION_SECRET`, and writes
   `APP_ADMIN_PASSWORD_HASH`, the argon2id hash computed by `ads-engine hash-password` in the
   backend image. Every other variable keeps its safe default, so `APP_ADMIN_EMAIL` stays
   `admin@example.com` and no source has credentials. To keep the password out of your shell
   history, see [First start](docs/runbook.md#first-start) in the runbook.

2. Build and start the stack:

   ```bash
   docker compose up -d --build
   ```

   `make up` does the same after `make check-tools`, then waits up to `HEALTH_TIMEOUT` (default
   180) seconds for `/api/v1/healthz` to answer 200. Startup order:

   ```text
   db (healthy) --> migrate (exit 0) --> api (healthy) --> web
                                     \-> worker
   ```

   - `db` (`postgres:18.6`) is healthy once `pg_isready` answers.
   - `migrate` is one-shot: `ads-engine migrate`, then `ads-engine ensure-admin`, which creates
     or updates the admin from `.env`.
   - `api` and `worker` start only after `migrate` succeeds.
   - `web` (nginx) starts once `api` is healthy, serves the SPA on port 8080 and proxies `/api/`.
     Port 8080 is published on `127.0.0.1` only, so no other machine reaches it.

3. Open <http://localhost:8080> and sign in as `admin@example.com` with the password from step 1.
   You land on `/actions`. It shows "No analysis run yet" until the worker's first default run
   completes, usually within a few minutes, and then lists all five sections on simulated data.

With no source credentials, every source resolves to simulated data with the reason
"credentials not configured". The worker builds and backfills a simulated dataset per source and
runs the default analysis before its first scheduled cycle. Simulated data is seeded
(`MOCK_SEED`, default 42): the same seed, scenario, configuration and clock reproduce it exactly,
and the tests fix the clock to pin their expected numbers. The running product sets no anchor date:
its world is anchored to the current UTC date when it is built, so a stack first started on a later
day gets its dates rolled forward and different numbers. Simulated data is rendered in each source's
live wire contract, so the same clients, parsers and rules run unchanged. Everything simulated is
labelled: a "Simulated" chip per source, a global banner naming each simulated source and its
reason, and a "Simulated data" flag on every affected item.

## Going live

Each source goes live on its own, with no code change:

1. Add the source's variables to `.env`:

   | Source | Required | Optional |
   | --- | --- | --- |
   | Google Ads | `N8N_GOOGLE_ADS_WEBHOOK_URL`, `N8N_WEBHOOK_SECRET`, `GOOGLE_ADS_CUSTOMER_ID` (10 digits; dashes are stripped) | `N8N_WEBHOOK_SECRET_HEADER`, `GOOGLE_DAILY_CALL_BUDGET` |
   | LinkedIn | `LINKEDIN_AD_ACCOUNT_IDS`, plus either `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` and `LINKEDIN_REFRESH_TOKEN`, or `LINKEDIN_ACCESS_TOKEN` | None |
   | HubSpot | `HUBSPOT_ACCESS_TOKEN` | `HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY` and `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`, both set, to link Google Ads contacts to the CRM |

2. Recreate the processes so they read the new environment. `docker compose restart` is not
   enough, because a restarted container keeps the environment it was created with:

   ```bash
   docker compose up -d --force-recreate api worker
   ```

3. The worker probes the source's full capability set. When every check passes, it builds a live
   dataset, backfills it over `BACKFILL_DAYS`, and only then activates it, in one transaction
   that retires the simulated dataset. Until activation the simulated dataset stays active and
   labelled.

- **Partial permissions keep the source simulated**: for example, LinkedIn ads readable while
  Lead Sync returns 403. The reason on `/sources` names the failing endpoint, and a recovery probe
  runs every 30 minutes.
- **Keep `DATA_MODE=auto`** (the default) while connecting sources. `DATA_MODE=simulated` forces
  every source to simulated data and never probes or promotes a source.

Probes, failure handling, token bootstrap and HubSpot scopes:
[Connecting sources](docs/runbook.md#connecting-sources) in the runbook.

## Environment variables

`.env.example` lists every variable; `make init` copies it to `.env`.

| Group | Variable | Type / format | Default in `.env.example` | Meaning |
| --- | --- | --- | --- | --- |
| Runtime | `POSTGRES_DB` | Database name | `ads_engine` | Database the `db` service creates |
| Runtime | `POSTGRES_USER` | Role name | `ads` | Database role the `db` service creates |
| Runtime | `POSTGRES_PASSWORD` | Secret string | `ads` (local only) | Role password; change it for any shared host |
| Runtime | `DATABASE_URL` | Secret SQLAlchemy URL with driver `postgresql+psycopg`; `postgresql://` and `postgres://` are rewritten to it, any other driver is refused | `postgresql+psycopg://ads:ads@db:5432/ads_engine` | Database that `migrate`, `api` and `worker` connect to; must match `POSTGRES_*` |
| Runtime | `APP_BASE_URL` | Absolute `http://` or `https://` URL with a host; a trailing `/` is dropped | `http://localhost:8080` | Public origin; `http://` is accepted only for `localhost` or `127.0.0.1` |
| Runtime | `DISPLAY_TIMEZONE` | IANA time zone name, such as `UTC` or `America/New_York` | `UTC` | Zone for the schedule and displayed instants; never moves a source's day boundary |
| Runtime | `DATA_MODE` | `auto` or `simulated` (case-insensitive) | `auto` | `auto`: each source live with automatic simulated fallback; `simulated`: every source simulated |
| Runtime | `MOCK_SEED` | Integer | `42` | Seed of the simulated data |
| Runtime | `BACKFILL_DAYS` | Positive integer | `180` | Initial backfill of every new dataset, in days, capped at 180 |
| Resources | `DB_CPU_LIMIT` | CPU cores, fractional allowed (`0.5`) | `1` | CPU ceiling of `db`; read by Compose only |
| Resources | `DB_MEMORY_LIMIT` | Size with `k`, `m` or `g` suffix (`1536m`) | `1g` | Memory ceiling of `db`; read by Compose only |
| Resources | `MIGRATE_CPU_LIMIT` | CPU cores, fractional allowed (`0.5`) | `1` | CPU ceiling of `migrate`; read by Compose only |
| Resources | `MIGRATE_MEMORY_LIMIT` | Size with `k`, `m` or `g` suffix (`1536m`) | `512m` | Memory ceiling of `migrate`; read by Compose only |
| Resources | `API_CPU_LIMIT` | CPU cores, fractional allowed (`0.5`) | `1` | CPU ceiling of `api`; read by Compose only |
| Resources | `API_MEMORY_LIMIT` | Size with `k`, `m` or `g` suffix (`1536m`) | `1g` | Memory ceiling of `api`; read by Compose only |
| Resources | `WORKER_CPU_LIMIT` | CPU cores, fractional allowed (`0.5`) | `1` | CPU ceiling of `worker`; read by Compose only |
| Resources | `WORKER_MEMORY_LIMIT` | Size with `k`, `m` or `g` suffix (`1536m`) | `2g` | Memory ceiling of `worker`; read by Compose only |
| Resources | `WEB_CPU_LIMIT` | CPU cores, fractional allowed (`0.5`) | `1` | CPU ceiling of `web`; read by Compose only |
| Resources | `WEB_MEMORY_LIMIT` | Size with `k`, `m` or `g` suffix (`1536m`) | `256m` | Memory ceiling of `web`; read by Compose only |
| Google | `N8N_GOOGLE_ADS_WEBHOOK_URL` | Absolute `https://` URL; `http://` only on `localhost` or `127.0.0.1` | Empty | Production URL of the n8n webhook |
| Google | `N8N_WEBHOOK_SECRET_HEADER` | HTTP header name (RFC 7230 token characters) | `X-Webhook-Token` | Header that carries the secret; must equal the Name on n8n's Header Auth credential. A blank value falls back to the code default `X-Webhook-Secret` |
| Google | `N8N_WEBHOOK_SECRET` | Secret string | Empty (secret) | Value of that header |
| Google | `GOOGLE_ADS_CUSTOMER_ID` | 10 digits; dashes are accepted and removed | Empty | Google Ads customer id, used without dashes |
| Google | `GOOGLE_DAILY_CALL_BUDGET` | Positive integer | `1000` | The product's own cap on webhook calls per sliding 24 h |
| LinkedIn | `LINKEDIN_AD_ACCOUNT_IDS` | Comma-separated numeric ids or `urn:li:sponsoredAccount:<id>` URNs; blank entries are dropped, duplicates removed | Empty | LinkedIn ad account ids |
| LinkedIn | `LINKEDIN_CLIENT_ID` | String | Empty | OAuth client id of the refresh-token trio |
| LinkedIn | `LINKEDIN_CLIENT_SECRET` | Secret string | Empty (secret) | OAuth client secret of the trio |
| LinkedIn | `LINKEDIN_REFRESH_TOKEN` | Secret string | Empty (secret) | Refresh token of the trio, exchanged for access tokens held in memory only |
| LinkedIn | `LINKEDIN_ACCESS_TOKEN` | Secret string | Empty (secret) | Static access token, the alternative to the trio; the trio wins when both are set |
| HubSpot | `HUBSPOT_ACCESS_TOKEN` | Secret string | Empty (secret) | Service Key or private-app token with read scopes only |
| HubSpot | `HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY` | Contact property name | Empty | Optional contact property holding the Google campaign id |
| HubSpot | `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY` | Name of a `datetime` contact property; a `date` property leaves the Google link unavailable | Empty | Optional contact property holding when that Google touch happened |
| Admin and session | `APP_ADMIN_EMAIL` | Email address (`local@domain`), lowercased | `admin@example.com` | The single admin login that `ensure-admin` creates or updates |
| Admin and session | `APP_ADMIN_PASSWORD_HASH` | Secret argon2 PHC string, single-quoted because it contains `$` | Empty; written by `make init` | argon2id hash of the admin password |
| Admin and session | `SESSION_SECRET` | Secret string; `make init` writes 64 hex characters (32 random bytes) | Empty; written by `make init` | Random key that signs the session cookie |

- **Blank means unset**: a blank value takes the default.
- **Database values travel together**: if you change any `POSTGRES_*` value, change
  `DATABASE_URL` to match.
- **`.env` is gitignored** and must never be committed. Compose passes it to `migrate`, `api` and
  `worker` only; `db` receives only the `POSTGRES_*` values.
- **Secrets stay on the server**: they live only in server-side environment variables, never in
  the browser bundle. No `VITE_` variable exists, so no secret can reach the SPA.
- **Resource limits are Compose variables**: `<SERVICE>_CPU_LIMIT` and `<SERVICE>_MEMORY_LIMIT`
  for `db`, `migrate`, `api`, `worker` and `web` (for example `WORKER_MEMORY_LIMIT`) cap each
  container; no process reads them. `.env.example` carries the `docker-compose.yml` defaults;
  change one in `.env` or the shell, as [Resource limits](docs/runbook.md#resource-limits)
  describes.

Who reads each variable, and how to rotate secrets:
[Environment and secrets](docs/runbook.md#environment-and-secrets) in the runbook.

## Make targets

`make` with no target, or `make help`, lists them.

| Target | What it does |
| --- | --- |
| `help` | Lists the targets (the default goal) |
| `check-tools` | Verifies Docker Compose `>=5.5.1,<6` and Docker Engine `>=29`, printing the required range on failure |
| `init` | First start (`make init ADMIN_PASSWORD=<value>`): copies `.env.example` to `.env` without overwriting one, and writes `SESSION_SECRET` and `APP_ADMIN_PASSWORD_HASH` |
| `up` | Builds and starts the stack detached (`docker compose up -d --build`), then waits for `/api/v1/healthz` |
| `down` | Stops and removes the containers; the database volume `pgdata` is kept (`docker compose down -v` deletes it) |
| `migrate` | Runs the one-shot `migrate` service: `ads-engine migrate && ads-engine ensure-admin` |
| `test` | Backend pytest against a disposable `postgres:18.6` container, then the web Vitest suite. On exit, Ctrl-C included, it removes that container and its anonymous volume and checks for up to about 10 s that they are gone; a removal it cannot confirm fails the target, even when the tests passed, and prints the `docker` command that finds or removes what remains |
| `lint` | `ruff format --check`, `ruff check` and `mypy --strict src`; ESLint, stylelint and `tsc --noEmit` |
| `e2e` | Playwright against the Compose stack started with `DATA_MODE=simulated` (`make e2e ADMIN_PASSWORD=<value>`); leaves the stack running |
| `seed-simulated` | Seeds simulated datasets for every source and computes the default-window run |
| `secret-scan` | Builds the local scanner image `ads-engine-gitleaks:v8.30.1-patched` (never pushed) from the pinned `zricethezav/gitleaks:v8.30.1`, upgrading only the Alpine libraries that have published security fixes to at least the fixed versions, then runs gitleaks `v8.30.1` from it, with no network, and `.gitleaks.toml` over every commit; a finding fails it |

Overridable variables (`COMPOSE`, `APP_URL`, `HEALTH_TIMEOUT`, `TEST_DB_PORT`, `PYTEST_ARGS`,
`PLAYWRIGHT_INSTALL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`) are
described at the top of the `Makefile` and in [Make targets](docs/runbook.md#make-targets) in the
runbook.

## Configuration files

The three files in `config/` are bind-mounted read-only at `/app/config` in the backend
containers, so an edit needs no rebuild:

- **`config/thresholds.yaml`**: every rule threshold, plus the analysis window. Defaults:
  `window.default_days` 28, `window.trusted_min_days` 14 (shorter windows are flagged),
  `window.presets` `[14, 28, 56, 90]` and `window.max_days` 180.
- **`config/content_assets.yaml`**: the white paper registry. Each asset has a slug, a title, its
  canonical LinkedIn lead-form URNs and optional creative overrides. The shipped entries match the
  simulated world; replace them with your account's form and creative URNs for live data.
- **`config/hubspot_mapping.yaml`**: the MQL binding (default: the lifecycle stage labelled
  "First meeting booked", or a date property with `mode: property`), the QL stage labels
  ("Qualified Lead", then "Sales Qualified Lead"), the booking sources (`crm.booking_sources`,
  default `MEETINGS_PUBLIC`) and the meeting outcome exclusions
  (`crm.meeting_outcome_exclusions`, default `CANCELED`).

After an edit, run `docker compose restart worker api`. The parsed values of all three files form
`config_hash`, which is stored on every analysis run and shown on `/settings`; when it changes,
the worker queues a fresh default-window run. Every rule and threshold key is explained in
[docs/recommendation-rules.md](docs/recommendation-rules.md).

## Security

- **Login**: a single admin login. The session cookie is signed, HttpOnly, `SameSite=Lax` and
  lasts 12 h; it is marked Secure whenever `APP_BASE_URL` is `https://`.
- **HTTPS beyond localhost**: plain HTTP is accepted only for a `localhost` or `127.0.0.1`
  `APP_BASE_URL`, and Compose publishes nginx on `127.0.0.1:8080` only, so the plain-HTTP login
  never reaches the network. Any other host needs an `https://` `APP_BASE_URL` and a TLS
  terminator in front of nginx that redirects HTTP to HTTPS and sends HSTS
  ([Hosting beyond localhost](docs/runbook.md#hosting-beyond-localhost)).
- **CSRF**: every mutating request must carry `X-Requested-With: ads-engine`.
- **Brute force**: sign-in is limited to 5 attempts per minute per client IP.
- **Personal data**: lead emails are persisted only as SHA-256 hashes, and free-text form answers
  are not stored.
- **Logs**: structured JSON with the n8n secret header and `Authorization` values redacted.
- **Least privilege**: LinkedIn and HubSpot access uses read-only scopes.

## Development and CI

Backend, from the repository root:

**Warning: the backend tests erase their database.** `uv run pytest` needs a PostgreSQL 18
database at `TEST_DATABASE_URL` (default
`postgresql+psycopg://ads:ads@localhost:55432/ads_engine_test`). Once per run it drops schema
`public` with everything in it, then recreates and migrates it, and it truncates every table before
each database test. Point it only at a dedicated, disposable database that you alone use, never at
the application's database (`DATABASE_URL`, kept in the Compose `pgdata` volume), a production
database, or one shared with other people or concurrent runs. With `TEST_DATABASE_URL` unset, the
tests erase `ads_engine_test` on whatever server listens on `localhost:55432`. Prefer `make test`:
it starts its own disposable `postgres:18.6` container on `127.0.0.1:TEST_DB_PORT` (default 55432;
`0` lets Docker choose a free port), points `TEST_DATABASE_URL` at it in place of any value you
set, and runs the backend suite and then the web unit tests.

```bash
cd backend
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy --strict src
uv run pytest
```

The suite enforces at least 80% line coverage overall and 90% on `ads_engine/analysis/` and the
connector parsers.

Web, from the repository root:

```bash
cd web
npm ci
npm run lint
npm run typecheck
npm test
npm run build
```

`npm test` runs Vitest with at least 70% statement coverage overall and 80% on
`src/features/actions/`.

CI (`.github/workflows/ci.yml`) runs on every push and pull request:

- lint and format checks (ruff; ESLint and stylelint);
- type checks (`mypy --strict`; `tsc --noEmit`);
- backend tests against a `postgres:18.6` service container, with the coverage gates;
- web unit tests (Vitest), with the coverage thresholds;
- the SPA build (`npm run build`), then the three Compose images (`docker compose build`): the
  backend and web images and `db`'s inline `postgres:18.6` build, once Compose 5.5.1 is installed
  (SHA-256 checked) and `make check-tools` passes;
- Playwright end-to-end tests on the documented first start (`make init`, then
  `docker compose up -d --build`) with no source credentials, so every source runs on simulated
  data;
- the gitleaks `v8.30.1` secret scan over every commit.

The lockfiles `backend/uv.lock` and `web/package-lock.json` are committed, and CI installs with
`uv sync --frozen` and `npm ci`. Nothing is published or deployed.

## Design system

The UI is built only on `web/src/design-system/`, an indirection layer for the Blitzy OS design
system. The Blitzy OS source was not supplied, so every component wrapper and token value is a
placeholder, listed in [web/src/design-system/GAPS.md](web/src/design-system/GAPS.md). Binding
the real system changes only that folder plus `web/package.json` and `web/package-lock.json`.

## Out of scope

- Writing to LinkedIn, Google Ads or HubSpot: no automatic pausing, budget edits or negative
  keywords.
- Changes to the n8n workflow or its response.
- Direct Google Ads access: no Google OAuth, client credentials or developer token.
- Google ad copy and responsive search ad analysis.
- Performance Max search terms and Keyword Planner ideas.
- Multi-tenant use, role-based access beyond the single admin, and a LinkedIn OAuth consent UI.
- Notifications: email, Slack or scheduled digests.
- Production deployment automation: cloud infrastructure-as-code, image publishing, deployment.
- Monitoring beyond structured logs and `/api/v1/healthz`.

## Documentation

- [docs/architecture.md](docs/architecture.md): components, data flow, schedule, locks,
  datasets, fallback and security.
- [docs/recommendation-rules.md](docs/recommendation-rules.md): every rule, guard, precedence
  step, threshold key, flag and finding.
- [docs/integrations/google-ads-n8n.md](docs/integrations/google-ads-n8n.md): webhook contract,
  queries, partitioning and the call budget.
- [docs/integrations/linkedin.md](docs/integrations/linkedin.md): versions, endpoints, analytics,
  segments and Lead Sync.
- [docs/integrations/hubspot.md](docs/integrations/hubspot.md): stage binding, lead matching,
  associations and deal search.
- [docs/runbook.md](docs/runbook.md): setup, secrets, LinkedIn token bootstrap, simulated mode,
  troubleshooting and recovery.
