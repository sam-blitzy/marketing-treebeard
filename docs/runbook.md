# Ads Action Engine runbook

This runbook is for the operator who installs, configures, connects and recovers the Ads Action
Engine on a single host. Every command runs from the repository root unless its block says
otherwise, and every value shown as `<value>` is a placeholder: never paste a real secret into a
document, an issue or a commit.

The product only reads from LinkedIn, Google Ads and HubSpot. It never pauses a campaign, edits a
budget or writes a record: the operator applies each change in the platform and records it under
Decisions.

Related documents: [architecture](architecture.md) (components, schedule, datasets, fallback),
[recommendation rules](recommendation-rules.md) (every rule, threshold and flag), the integration
notes for [Google Ads via n8n](integrations/google-ads-n8n.md),
[LinkedIn](integrations/linkedin.md) and [HubSpot](integrations/hubspot.md), and the
[project README](../README.md).

## Contents

- [Prerequisites](#prerequisites)
- [First start](#first-start)
- [Admin login](#admin-login)
- [Environment and secrets](#environment-and-secrets)
- [Connecting sources](#connecting-sources)
- [Simulated mode](#simulated-mode)
- [Reading `/sources`](#reading-sources)
- [Troubleshooting](#troubleshooting)
- [Operating commands](#operating-commands)
- [Capturing live contract fixtures](#capturing-live-contract-fixtures)
- [Changing thresholds, mappings and the registry](#changing-thresholds-mappings-and-the-registry)
- [Hosting beyond localhost](#hosting-beyond-localhost)
- [Database reset and backup](#database-reset-and-backup)
- [Tests and quality checks](#tests-and-quality-checks)
- [Make targets](#make-targets)

## Prerequisites

| Tool | Version | Needed for |
|------|---------|------------|
| Docker Engine | 29 or newer (`ENGINE_MIN` in the `Makefile`) | Every container |
| Docker Compose plugin | 5.5.1 or newer, below 6 (`COMPOSE_MIN`, `COMPOSE_MAX`) | The stack in `docker-compose.yml` |
| GNU make | 4.0 or newer, with bash | Every `make` target. On macOS, `brew install make` and run `gmake` |
| POSIX tools | `awk`, `od`, `mktemp`, and `curl` or `wget` | `make init` and the health poll of `make up` and `make e2e` |
| git | Any current release | Cloning, and `make secret-scan` |
| uv | Exactly 0.12.21 (`required-version` in `backend/pyproject.toml`) | Host-side backend tests and lint only |
| Python | 3.14 (`requires-python = ">=3.14,<3.15"`) | Host-side backend tests and lint only |
| Node.js | 24.21.0 or newer (`engines` in `web/package.json`) | Host-side web tests, lint and end-to-end tests only |

The running product needs only Docker, Compose and make: the backend and web images bring their
own Python, Node and nginx. uv, Python and Node matter only for `make test`, `make lint` and
`make e2e`, which run tools on the host.

`make check-tools` verifies the Docker versions. On failure it names what it found and the
required range:

```bash
make check-tools
# for example: Docker Compose 5.5.1 (>=5.5.1,<6) and Docker Engine 29.7.2 (>=29): ok
```

## First start

1. Check the tools:

   ```bash
   make check-tools
   ```

2. Write `.env` with a session secret and the admin password hash. The leading space keeps the
   command out of bash history where `HISTCONTROL` is `ignorespace` or `ignoreboth`:

   ```bash
    make init ADMIN_PASSWORD='<choose a password>'
   ```

   To keep the password out of the command line and the history entirely, read it first and
   pass it through the environment, which `make init` accepts unchanged:

   ```bash
   read -rs -p 'Admin password: ' ADMIN_PASSWORD; echo
   ADMIN_PASSWORD="$ADMIN_PASSWORD" make init
   unset ADMIN_PASSWORD
   ```

   `make init` then:
   - runs `make check-tools`;
   - refuses to run when `.env` already exists ("refusing to overwrite existing .env; move it
     aside first to initialise again");
   - copies `.env.example` to `.env`, readable by the owner only (mode 600);
   - writes `SESSION_SECRET`, 32 random bytes as 64 hex characters from `/dev/urandom`;
   - builds the backend image and writes `APP_ADMIN_PASSWORD_HASH`, the argon2id hash printed
     by `ads-engine hash-password`, which reads the password on stdin. The hash is single-quoted
     in `.env`, because Compose would otherwise interpolate its `$` signs;
   - leaves every other variable at its `.env.example` default, so `APP_ADMIN_EMAIL` stays
     `admin@example.com` and no source has credentials;
   - prints "written" instead of either generated value, and removes the partial `.env` if any
     step fails.

3. Build and start the stack:

   ```bash
   docker compose up -d --build
   ```

   `make up` does the same after `make check-tools`, then waits up to `HEALTH_TIMEOUT` (default
   180) seconds for `http://localhost:8080/api/v1/healthz` to answer 200, and prints the
   containers' state and recent logs when it does not. Startup order:

   ```text
   db (healthy) --> migrate (exit 0) --> api (healthy) --> web
                                     \-> worker
   ```

   - `db` (`postgres:18.6`) is healthy once `pg_isready` answers over TCP.
   - `migrate` is one-shot: `ads-engine migrate`, then `ads-engine ensure-admin`. Both are
     idempotent, so it runs again on every `up`.
   - `api` and `worker` start only after `migrate` exits 0. `api` is healthy once
     `GET /api/v1/healthz` answers 200, which includes a database round trip.
   - `web` (nginx) starts once `api` is healthy. It serves the SPA on port 8080 and proxies
     `/api/` to the API. Port 8080 is the only published port.

4. Open <http://localhost:8080> and sign in as `admin@example.com` with the chosen password.

### Without source credentials

A fresh `.env` holds no source credentials, and that is not an error:

- Every source resolves to simulated data with the reason "credentials not configured".
- The worker builds and backfills a simulated dataset per source (`BACKFILL_DAYS`, default 180),
  then computes the default-window analysis before its first scheduled cycle.
- `/actions` shows "No analysis run yet" until that first run succeeds, which can take a few
  minutes, then lists all five sections on simulated data. The page refreshes by itself.
- On a local `http://localhost` URL the session cookie is not marked Secure. That is expected
  only for localhost; any other host needs HTTPS ([Hosting beyond localhost](#hosting-beyond-localhost)).

Connect real sources one at a time afterwards ([Connecting sources](#connecting-sources)).

## Admin login

The product has a single admin login. `ads-engine ensure-admin`, which the `migrate` service runs,
creates or updates it from `APP_ADMIN_EMAIL` and `APP_ADMIN_PASSWORD_HASH` and removes any other
login. Sign-in is limited to 5 attempts per minute per client address; the sixth gets 429, so wait
a minute before trying again.

**Change the password** (also the procedure for a lost password): hash the new password in the
backend image, write the hash into `.env` single-quoted, then re-run `migrate`. This block keeps
the password and the hash off the screen and out of the history:

```bash
read -rs -p 'New admin password: ' ADMIN_PASSWORD; echo
ADMIN_HASH="$(printf '%s' "$ADMIN_PASSWORD" | docker compose run --rm -T --no-deps migrate ads-engine hash-password)"
unset ADMIN_PASSWORD
case "$ADMIN_HASH" in
  '$argon2id$'*)
    tmp_env="$(mktemp .env.XXXXXX)"
    ADMIN_HASH="$ADMIN_HASH" awk '
      BEGIN { q = sprintf("%c", 39) }
      /^APP_ADMIN_PASSWORD_HASH=/ { print "APP_ADMIN_PASSWORD_HASH=" q ENVIRON["ADMIN_HASH"] q; next }
      { print }
    ' .env > "$tmp_env" && chmod 600 "$tmp_env" && mv -f "$tmp_env" .env && echo "APP_ADMIN_PASSWORD_HASH written"
    ;;
  *) echo "hash-password failed; .env unchanged" >&2 ;;
esac
unset ADMIN_HASH
make migrate
```

`docker compose run --rm -T --no-deps migrate ads-engine hash-password` is the same call
`make init` makes: `-T` attaches stdin without a terminal, and `--no-deps` leaves the database
untouched. `make migrate` then runs `ads-engine migrate && ads-engine ensure-admin`, which stores
the new hash. Sign-in reads the stored hash, so `api` needs no restart.

**Change the email**: edit `APP_ADMIN_EMAIL` in `.env`, then run `make migrate`. `ensure-admin`
creates the login under the new email and removes the old one.

## Environment and secrets

All configuration that is not a threshold or a mapping lives in `.env`, which `make init` copies
from `.env.example`.

- **Who reads it**: Compose injects `.env` into `migrate`, `api` and `worker` only. A missing `.env`
  stops Compose before any container starts. `db` receives only `POSTGRES_DB`, `POSTGRES_USER` and
  `POSTGRES_PASSWORD`, interpolated from the shell, else from `.env`, else the local defaults.
  `web` receives no environment at all, so the browser bundle holds no configuration and no
  secret, and no `VITE_` variable exists.
- **No file reads**: the processes read only their environment. `settings.py` never opens a `.env`
  file itself.
- **Blank means unset**: `KEY=` or a value of only spaces takes the default below.
- **Format**: plain `KEY=value` lines, with comments on their own lines. Single-quote a value that
  contains `$`, as `make init` does for the hash, because Compose interpolates `$` in unquoted and
  double-quoted values.
- **`DATA_MODE` from the shell wins**: `DATA_MODE=simulated docker compose up -d` overrides `.env`.

| Variable | Group | Default | Secret | Read by | Purpose |
|----------|-------|---------|--------|---------|---------|
| `POSTGRES_DB` | Runtime | `ads_engine` | No | `db` | Database the `db` service creates. Must match `DATABASE_URL` |
| `POSTGRES_USER` | Runtime | `ads` | No | `db` | Database role the `db` service creates. Must match `DATABASE_URL` |
| `POSTGRES_PASSWORD` | Runtime | `ads` (local only) | Yes | `db` | Role password. Applied only when the `pgdata` volume is first initialised ([Hosting beyond localhost](#hosting-beyond-localhost)) |
| `DATABASE_URL` | Runtime | `postgresql+psycopg://ads:ads@db:5432/ads_engine` | Yes | `migrate`, `api`, `worker` | SQLAlchemy URL. Host `db` is the Compose service. Percent-encode special characters in the password |
| `APP_BASE_URL` | Runtime | `http://localhost:8080` | No | `api` | Public origin. `http://` is accepted only for `localhost` or `127.0.0.1`; `https://` makes the session cookie Secure |
| `DISPLAY_TIMEZONE` | Runtime | `UTC` | No | `api`, `worker` | IANA zone for the schedule's wall-clock slots and for displayed instants. Never moves a source's day boundary |
| `DATA_MODE` | Runtime | `auto` | No | `migrate`, `api`, `worker` | `auto`: each source live, with automatic simulated fallback. `simulated`: every source simulated, never probed |
| `MOCK_SEED` | Runtime | `42` | No | `worker` | Seed of the simulated world, so simulated data reproduces exactly |
| `BACKFILL_DAYS` | Runtime | `180` | No | `worker` | Initial backfill of every new dataset, in days; capped at 180 for every source (LinkedIn's 6-month daily retention) |
| `DB_CPU_LIMIT` | Resources | `1` | No | Compose (`deploy.resources.limits`) | CPU ceiling of `db`, in cores ([Resource limits](#resource-limits)) |
| `DB_MEMORY_LIMIT` | Resources | `1g` | No | Compose (`deploy.resources.limits`) | Memory ceiling of `db` |
| `MIGRATE_CPU_LIMIT` | Resources | `1` | No | Compose (`deploy.resources.limits`) | CPU ceiling of `migrate`, in cores |
| `MIGRATE_MEMORY_LIMIT` | Resources | `512m` | No | Compose (`deploy.resources.limits`) | Memory ceiling of `migrate` |
| `API_CPU_LIMIT` | Resources | `1` | No | Compose (`deploy.resources.limits`) | CPU ceiling of `api`, in cores |
| `API_MEMORY_LIMIT` | Resources | `1g` | No | Compose (`deploy.resources.limits`) | Memory ceiling of `api` |
| `WORKER_CPU_LIMIT` | Resources | `1` | No | Compose (`deploy.resources.limits`) | CPU ceiling of `worker`, in cores |
| `WORKER_MEMORY_LIMIT` | Resources | `2g` | No | Compose (`deploy.resources.limits`) | Memory ceiling of `worker` |
| `WEB_CPU_LIMIT` | Resources | `1` | No | Compose (`deploy.resources.limits`) | CPU ceiling of `web`, in cores |
| `WEB_MEMORY_LIMIT` | Resources | `256m` | No | Compose (`deploy.resources.limits`) | Memory ceiling of `web` |
| `N8N_GOOGLE_ADS_WEBHOOK_URL` | Google | Unset | Sensitive, never logged | `worker` | Production URL of the existing n8n webhook. Must be `https://` except on `localhost` or `127.0.0.1` |
| `N8N_WEBHOOK_SECRET_HEADER` | Google | `X-Webhook-Token` in `.env.example`; `X-Webhook-Secret` in code when blank | No | `worker` | Name of the header that carries the secret. Must equal the Name on the n8n Header Auth credential |
| `N8N_WEBHOOK_SECRET` | Google | Unset | Yes | `worker` | Value of that header |
| `GOOGLE_ADS_CUSTOMER_ID` | Google | Unset | No | `worker` | Google Ads customer id. Dashes are stripped; the result must be exactly 10 digits |
| `GOOGLE_DAILY_CALL_BUDGET` | Google | `1000` | No | `worker`, `api` | The product's own cap on webhook calls per sliding 24 h, shown on `/sources` |
| `LINKEDIN_AD_ACCOUNT_IDS` | LinkedIn | Unset | No | `worker` | Comma-separated numeric ad account ids (`urn:li:sponsoredAccount:<id>` entries are also accepted) |
| `LINKEDIN_CLIENT_ID` | LinkedIn | Unset | No | `worker` | OAuth client id of the refresh-token trio |
| `LINKEDIN_CLIENT_SECRET` | LinkedIn | Unset | Yes | `worker` | OAuth client secret of the trio |
| `LINKEDIN_REFRESH_TOKEN` | LinkedIn | Unset | Yes | `worker` | Refresh token of the trio, exchanged for access tokens held in memory only |
| `LINKEDIN_ACCESS_TOKEN` | LinkedIn | Unset | Yes | `worker` | Static access token, the alternative to the trio. The trio wins when both are set |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot | Unset | Yes | `worker` | Service Key or private-app token with read scopes only |
| `HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY` | HubSpot | Unset | No | `worker` | Optional contact property holding the Google campaign id |
| `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY` | HubSpot | Unset | No | `worker` | Optional `datetime` contact property holding when that Google touch happened |
| `APP_ADMIN_EMAIL` | Admin and session | `admin@example.com` | No | `migrate` | The admin login that `ensure-admin` creates or updates |
| `APP_ADMIN_PASSWORD_HASH` | Admin and session | Unset; written by `make init` | Yes | `migrate` | argon2id hash of the admin password, single-quoted |
| `SESSION_SECRET` | Admin and session | Unset; written by `make init` | Yes | `api` | Key that signs session cookies. The API refuses to start without it |

The CLI commands read the same variables inside the backend containers; `ads-engine --help` lists
what each command needs.

### Secrets

- **Where they live**: only in `.env` on the server. `.env` and every `.env.*` variant except
  `.env.example` are gitignored. Never put a secret in `config/`, in `web/`, in a `VITE_`
  variable, in `docker-compose.yml` or in a commit.
- **How they are protected**: secret settings are pydantic `SecretStr` values, so they never print.
  Logs are structured JSON with redaction: `Authorization` values, the n8n secret header, every
  configured secret and email addresses are stripped. Lead emails are stored only as SHA-256
  hashes.
- **Rotating a source credential**: replace the value in `.env`, then recreate the processes that
  read it ([General procedure](#general-procedure)):

  ```bash
  docker compose up -d --force-recreate api worker
  ```

  `docker compose restart` is not enough: a restarted container keeps the environment it was
  created with.
- **Rotating `SESSION_SECRET`**: generate 32 random bytes, put them in `.env` as
  `SESSION_SECRET=<64 hex characters>`, and recreate `api`. Every open session is signed out.

  ```bash
  od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; echo
  ```

- **Before every push**: run the secret scan. It runs the pinned `zricethezav/gitleaks:v8.30.1`
  image with `.gitleaks.toml` over every commit, and any finding fails it. CI runs the same scan.

  ```bash
  make secret-scan
  ```

  The scan reads committed history, not the working tree. A `gitleaks dir` scan of a working tree
  would also report the gitignored `.env` by design.

### Resource limits

Each service runs under a CPU and memory ceiling, `deploy.resources.limits` in
`docker-compose.yml`. The limits are Compose variables, not product settings: `.env.example` lists
them with the defaults below and no process reads them. Compose takes each one from the shell,
else from `.env`, else the default below; a blank value keeps the default.

| Service | CPU variable | CPU default | Memory variable | Memory default |
|---------|--------------|-------------|-----------------|----------------|
| `db` | `DB_CPU_LIMIT` | `1` | `DB_MEMORY_LIMIT` | `1g` |
| `migrate` | `MIGRATE_CPU_LIMIT` | `1` | `MIGRATE_MEMORY_LIMIT` | `512m` |
| `api` | `API_CPU_LIMIT` | `1` | `API_MEMORY_LIMIT` | `1g` |
| `worker` | `WORKER_CPU_LIMIT` | `1` | `WORKER_MEMORY_LIMIT` | `2g` |
| `web` | `WEB_CPU_LIMIT` | `1` | `WEB_MEMORY_LIMIT` | `256m` |

- **Ceilings, not reservations**: an idle service holds nothing back. The memory defaults leave
  several times the peak use of a simulated first start with a 180-day backfill and a 180-day
  custom run. That start takes as long under the 1-core CPU defaults as with no limits: the worker
  is one Python process that peaks at about one core, and PostgreSQL at about a tenth of one.
- **CPU** is a number of cores and may be fractional (`0.5`). The 1-core defaults run on any host
  Docker supports. Docker refuses to create a container whose value exceeds the CPUs it reports
  (`docker info --format '{{.NCPU}}'`).
- **Memory** is a size with a `k`, `m` or `g` suffix (`1536m`). An invalid size stops Compose
  before any container starts. A service that exceeds its ceiling is killed with exit code 137
  ([Troubleshooting](#troubleshooting)).
- **One worker**: a higher limit gives the worker more room and never adds a second worker
  process; the scheduler's advisory lock keeps a single active instance
  ([Locks](architecture.md#locks)).

To raise a limit, set its variable in `.env`, then run `docker compose up -d`, which recreates the
services whose configuration changed. `docker inspect` shows the values in force, CPU in
billionths of a core and memory in bytes:

```dotenv
WORKER_MEMORY_LIMIT=3g
```

```bash
docker compose up -d
docker inspect --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}' "$(docker compose ps -q worker)"
```

## Connecting sources

Each source goes live on its own, and a source without working access stays on simulated data. Keep
`DATA_MODE=auto` (the default) while connecting sources: `DATA_MODE=simulated` never promotes a
source to live.

### General procedure

1. Add the source's variables to `.env` (sections below).
2. Recreate the processes so they read the new environment. Compose also re-runs `migrate`, whose
   environment changed too:

   ```bash
   docker compose up -d --force-recreate api worker
   ```

3. At startup the worker resolves the source as live and, because its active dataset is still
   simulated, probes the source's full capability set before building anything:
   - Google: query Q-G1 (customer id, currency, time zone) through the webhook.
   - LinkedIn, per ad account: `adAccounts/{id}`, a one-day `adAnalytics` request, `leadForms`
     with `count=1` and `leadFormResponses` with `count=1`.
   - HubSpot: a one-email contacts batch read, a one-result deals search and a one-input
     associations read.

   Probes of one source are at least 30 minutes apart (`PROBE_INTERVAL` in `sync/mode.py`). When
   the source's previous probe is younger, the next one waits for the interval. Each probe is
   listed on `/sources` as a `probe` sync run.
4. When every check passes, the worker builds a live dataset, backfills it over `BACKFILL_DAYS`
   (default 180, capped at 180 for every source by LinkedIn's 6-month daily retention), and only
   then activates it, in one transaction that retires the simulated dataset. Additionally:
   - a HubSpot build activates only once every staged lead match has been attempted in it and its
     `hubspot.*` freshness rows are recorded (staged-matching readiness);
   - a LinkedIn build stages its backfilled leads against HubSpot's active dataset and attempts
     every match before it activates.
5. Until activation, the simulated dataset stays active and labelled "Simulated". The activation
   queues a fresh default-window run. Until that run completes, cards computed from the replaced
   data show "Computed from replaced data", and every action on them is disabled.

When the probe fails, the classification of the failed checks decides what happens:

| Probe result | What the worker does | What to do |
|--------------|----------------------|------------|
| A denial on any check: 401, 403, or a Google denial inside an n8n 200 | Automatic fallback: the source stays on simulated data, with the platform code and failing endpoint as reason. A recovery probe runs every 30 minutes | Fix the access, then wait for the next probe, or fix `.env` and recreate the processes |
| A defect: 400, GAQL `QueryError`, validation | Status `error`, no simulated fallback, the defect logged with its query id | Inspect `docker compose logs worker` for the query id |
| Transient failures only: 429, 5xx, timeout, `RESOURCE_EXHAUSTED`, call budget | Status `stale`; the active dataset is kept; probed again after the interval | Usually nothing; retries are automatic |

The dataset lifecycle (build, activation, retirement and the 30-day purge) is described in
[architecture](architecture.md#dataset-lifecycle), and recovery in
[architecture](architecture.md#recovery).

### Google Ads through the existing n8n webhook

Google Ads is reached only through the existing n8n Cloud webhook. The product never holds a Google
client ID, client secret, refresh token or developer token, has no Google OAuth flow, and this
runbook never asks for any of them: they stay in n8n's credential store.

```dotenv
N8N_GOOGLE_ADS_WEBHOOK_URL=<production URL of the n8n webhook, https://...>
N8N_WEBHOOK_SECRET_HEADER=X-Webhook-Token
N8N_WEBHOOK_SECRET=<shared secret configured on the n8n Header Auth credential>
GOOGLE_ADS_CUSTOMER_ID=<10-digit customer id; dashes are stripped>
# Optional: the product's own cap on webhook calls per sliding 24 h (default 1000)
GOOGLE_DAILY_CALL_BUDGET=1000
```

`N8N_WEBHOOK_SECRET_HEADER` must equal the header Name on the n8n credential. `.env.example`
ships `X-Webhook-Token`, the name the existing credential expects; it refuses `X-Webhook-Secret`,
the code's default for a blank value, with 403, which the product reads as access denied.

Verify the existing workflow against this checklist. The product needs no change to the workflow
or to its response, and none is made from here:

- [ ] **Webhook node, HTTP Method**: `POST`.
- [ ] **Webhook node, Authentication**: `Header auth`, using a Header Auth credential whose
  **Name** equals `N8N_WEBHOOK_SECRET_HEADER` and whose **Value** equals `N8N_WEBHOOK_SECRET`. n8n
  rejects a request without the right header before the workflow runs.
- [ ] **Production URL**: `N8N_GOOGLE_ADS_WEBHOOK_URL` is the Production URL, and the workflow is
  published (activated), so n8n has registered it. The Test URL answers only while the editor
  listens.
- [ ] **Respond**: `When Last Node Finishes` with Response Data `First Entry JSON` (or
  `All Entries`, which wraps the same object in a one-element array the product also accepts), or
  `Using 'Respond to Webhook' Node` returning that object. Never `Immediately`, whose "Workflow got
  started" body carries no rows.
- [ ] **Response body**: the `googleAds:search` response as `{results[], fieldMask,
  queryResourceConsumption}`, answered synchronously.
- [ ] **Google Ads call**: one `googleAds:search` call on API v25 per request, never
  `searchStream`, built from the request body `{"customer_id", "query", "page_token"}` and using
  the existing Google Ads credential.

Quota: the Google Ads access level and its daily operation limit belong to the developer token
inside n8n's credential, and every other n8n workflow using that credential draws on the same
quota. The product cannot see it, so it caps its own calls with `GOOGLE_DAILY_CALL_BUDGET`
(default 1000), counting every webhook call it makes, retries and partition calls included.

| Access level | Production operations per 24 h |
|--------------|--------------------------------|
| Explorer | 2,880 |
| Basic | 15,000 |
| Standard | Unlimited |

Raise `GOOGLE_DAILY_CALL_BUDGET` above 1000 only when the token's level and the other workflows'
usage leave that headroom; lower it when they draw heavily. Calls beyond the cap are deferred to a
later 24 h window, never sent early. Details: [Google Ads via n8n](integrations/google-ads-n8n.md).

### LinkedIn token bootstrap

The product has no LinkedIn consent screen and runs no authorization-code flow. Tokens are
bootstrapped once, by hand, with the LinkedIn Developer Portal's Token Generator:

1. In the LinkedIn Developer Portal, use an app that has the **Advertising API** product (it
   starts at the Development tier and is upgraded to Standard) and the **Lead Sync API** product.
2. Make sure the app's OAuth scopes include the three read-only scopes the product needs:

   | Scope | Unlocks |
   |-------|---------|
   | `r_ads` | Ad accounts, campaign groups, campaigns and creatives |
   | `r_ads_reporting` | `adAnalytics`: daily metrics and demographic segments |
   | `r_marketing_leadgen_automation` | Lead forms and lead form responses (needs the Lead Sync API product; `r_ads` cannot read leads) |

3. Open the Token Generator, select the app, the OAuth flow and those three scopes, and sign in as
   the LinkedIn member who will authorise the product. That member needs a role on every ad
   account listed in `LINKEDIN_AD_ACCOUNT_IDS` that lets it read campaigns, reporting and leads.
4. Copy the access token and, where LinkedIn issues one to the app, the refresh token. LinkedIn
   offers programmatic refresh tokens to approved Marketing Developer Platform partners.
5. Put the values in `.env` and recreate the processes ([General procedure](#general-procedure)).
   With a refresh token, prefer the trio:

   ```dotenv
   LINKEDIN_AD_ACCOUNT_IDS=<numeric ad account id>,<another numeric ad account id>
   LINKEDIN_CLIENT_ID=<client id from the app's Auth tab>
   LINKEDIN_CLIENT_SECRET=<client secret from the app's Auth tab>
   LINKEDIN_REFRESH_TOKEN=<refresh token from the Token Generator>
   ```

   The worker exchanges the refresh token for access tokens itself and caches them in memory only.
   Without a refresh token, set `LINKEDIN_ACCESS_TOKEN=<access token>` instead; the trio wins when
   both are set.

Token lifetimes, as LinkedIn documents them: access tokens last 60 days, and programmatic refresh
tokens last 365 days from the original authorisation. Using a refresh token does not extend its
own lifetime, so repeat steps 3 to 5 before it expires. A static `LINKEDIN_ACCESS_TOKEN` must be
replaced by hand before its 60 days run out. An expired or revoked token is access denied, and
LinkedIn falls back to simulated data until the token is replaced.

- **One mode for all of LinkedIn**: a 403 on any LinkedIn endpoint keeps all of LinkedIn simulated.
  That includes a 403 on Lead Sync alone when `r_marketing_leadgen_automation` is not granted,
  because simulated leads cannot reference real creatives. The reason on `/sources` names the
  failing endpoint, for example `linkedin lead_form_responses: ACCESS_DENIED: Not enough
  permissions`.
- **Creative text**: headline and intro text come from the Posts API, which LinkedIn documents
  under `r_organization_social` with a page role of `ADMINISTRATOR`,
  `DIRECT_SPONSORED_CONTENT_POSTER` or `CONTENT_ADMIN`. One unreadable post only leaves that
  creative's text empty. A 403 on the whole posts call is a LinkedIn denial: the probe does not read
  posts, so it can pass while the live build's entity sync is denied, and LinkedIn then stays
  simulated with a posts endpoint as reason. Grant post read access to that member, or accept
  simulated LinkedIn data.

Details: [LinkedIn](integrations/linkedin.md).

### HubSpot

Set `HUBSPOT_ACCESS_TOKEN` to a Service Key (account level) or a private-app token. It needs read
scopes only; grant no write scope:

| Scope | Needed for |
|-------|------------|
| `crm.objects.contacts.read` | Contacts by email and by id, contact searches, and meetings, which HubSpot authorises through the contacts scope |
| `crm.objects.deals.read` | Deals by id and the deal search |
| `crm.schemas.contacts.read` | Startup verification of contact property definitions: `lifecyclestage`, the stage-date properties and the Google properties |

Associations between contacts, meetings and deals are read with the same object scopes. A missing
scope returns 403 `MISSING_SCOPES`, which is access denied: HubSpot stays simulated, and the reason
on `/sources` records the code and the failing endpoint. Grant exactly the read scopes HubSpot
names as missing.

```dotenv
HUBSPOT_ACCESS_TOKEN=<Service Key or private-app token>
# Optional Google-to-CRM link; both must be set
HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY=<contact property holding the Google campaign id>
HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY=<datetime contact property holding the Google touch instant>
```

- **Stage bindings**: at startup the worker resolves the MQL and QL bindings of
  `config/hubspot_mapping.yaml` against the portal's own `lifecyclestage` options and confirms
  each stage-date property. MQL defaults to the stage labelled "First meeting booked"; QL to
  "Qualified Lead", then "Sales Qualified Lead". An unresolved binding is shown as "unavailable",
  never counted as 0, and the stored reason names the missing stage or property. `/settings` shows
  the resolved properties; fix a binding as described under
  [Troubleshooting](#troubleshooting).
- **Google-to-CRM link**: it exists only when both Google properties are set and verified, and the
  touch-date property must have type `datetime`; a `date` property is refused. Without the link,
  Google items carry the `no_crm_link` flag, and no Google Qualified Lead count or Google pipeline
  veto is computed.

Details: [HubSpot](integrations/hubspot.md).

## Simulated mode

Simulated data renders a seeded world in each platform's live wire format, so the same clients,
parsers, rules and screens run without credentials. A source's active dataset is always entirely
live or entirely simulated.

- **`DATA_MODE=auto`** (the default) falls back per source: to simulated data with the reason
  "credentials not configured" when its variables are unset, or with the platform's code and the
  failing endpoint when access is denied.
- **`DATA_MODE=simulated`** forces every source to simulated data with the reason
  "DATA_MODE=simulated". It never probes and never promotes a source to live, even with valid
  credentials. Set it for one run from the shell, then return to the `.env` value:

  ```bash
  DATA_MODE=simulated docker compose up -d
  docker compose up -d
  ```

- **`MOCK_SEED`** (default 42) seeds the simulated world, so every run of the same seed produces
  the same data.
- **`make seed-simulated`** runs `ads-engine migrate && ads-engine seed-simulated` in a one-shot
  `migrate` container. It builds, backfills and activates a simulated dataset for every source,
  keeping one that is already simulated and active, then computes the default-window run. It needs
  no credentials, whatever `DATA_MODE` says. Run it only on a stack without live sources: a source
  that would otherwise be live gets a simulated dataset with the reason "simulated data seeded on
  request", which retires its live dataset, decisions included, until the worker's probe and build
  replace it with a new live dataset and fresh baseline decisions.

  ```bash
  make seed-simulated
  ```

What the labels mean:

- a **"Simulated" chip** per source in the status bar of `/actions`;
- a **global banner** naming each simulated source and its reason;
- a **"Simulated data" flag** on every item computed from simulated data;
- **`data_status`** on each item: `simulated`, or `mixed` when the item joins a live and a
  simulated source;
- simulated lead emails use the reserved `sim.invalid` domain and are never sent to a live HubSpot
  portal. While LinkedIn is simulated, its leads are matched only against a simulated companion CRM
  dataset, so simulated leads never join live contacts.

## Reading `/sources`

`/sources` shows one section per source (Google, LinkedIn, HubSpot):

- **Status line**: the mode ("Live" or "Simulated"), the connection status ("OK", "Stale",
  "Access denied" or "Error", for `ok`, `stale`, `denied` and `error`), the reason exactly as
  recorded (for example "credentials not configured") and the time of the last attempt.
- **Google call budget** (Google only): "Google calls in the last 24 h: N of M". N is
  `calls_24h`, the reserved webhook calls still counting in the sliding 24 h, and M is the cap,
  `GOOGLE_DAILY_CALL_BUDGET` (default 1000). N is a conservative upper bound and can exceed the
  POSTs actually sent: each call is booked two or three minutes ahead and counts until 24 h after
  its booked minute ends, slightly over 24 h in all, and a call whose reservation had to be
  renewed can count twice. The overcount only errs towards refusing a call, never towards
  exceeding the cap. A refusal sends nothing and adds nothing to N. This is the product's own
  cap, separate from the Google Ads quota other n8n workflows share.
- **Active datasets**: the primary dataset with its id, mode, state, activation time and reason
  and, for HubSpot while LinkedIn is simulated, the companion CRM dataset.
- **Dataset freshness**: per dataset, its completeness (`complete`, `incomplete` or `unknown`),
  covered range (`covered_from` to `covered_through`, native dates), last success, last attempt and
  reason. Covered dates are shown as stored, labelled with the source's day boundary, which also
  heads the tables ("Google days: America/New_York"): Google's is the account time zone ("day
  boundary not yet known" until the account is synced), LinkedIn's and HubSpot's are UTC. The
  display time zone applies to the instants only. The eleven datasets are:

  | Source | Datasets |
  |--------|----------|
  | Google | `google.entities`, `google.metrics_daily`, `google.search_terms` |
  | LinkedIn | `linkedin.entities`, `linkedin.analytics_daily`, `linkedin.segments`, `linkedin.leads` |
  | HubSpot | `hubspot.contacts`, `hubspot.associations`, `hubspot.deal_search`, `hubspot.google_contacts` (only when the Google-to-CRM link is configured and verified) |

- **Recent sync runs**: the newest `sync_run` rows, probes included.
- **Sync now**: queues a `sync_request` for that source. The worker checks for requests on every
  30 s wake, a check cadence rather than a deadline, and serves none before its
  [startup](architecture.md#worker-startup) has finished. Each source syncs in one of the worker's
  [lanes](architecture.md#lanes): `google`, or `linkedin_hubspot`, which serialises LinkedIn and
  HubSpot. When the source's lane is idle, the pass the next wake dispatches serves the request;
  when the lane is busy, the request is served once its running pass ends, so a long sync or
  backfill in that lane delays it. While one is pending, the button is disabled with "A sync is
  already pending", and the API answers a second request with 409 `sync_pending`.

How the freshness rows turn into item flags:

- An item is **stale** when a dataset it reads last succeeded more than `sources.stale_after_hours`
  (default 24) ago, or its coverage stops short of the window end.
- An item is **incomplete** when a dataset it reads is not `complete` over the window.
- A failed attempt alone flags nothing: the last good data, its coverage and its completeness stand.
- Reasons starting "call budget exhausted" or "interrupted" clear with a later fetch. "result limit
  reached at finest partition" (Google search terms) and "search result ceiling reached" (HubSpot
  CRM Search) are platform limits that stay flagged for the range they cover.

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `make init` prints "refusing to overwrite existing .env" | `.env` already exists | Keep it, or move it aside (`mv .env .env.old`) to initialise again. The old file holds secrets: delete it once nothing in it is needed |
| Compose stops before starting anything, or `make up` prints ".env not found" | No `.env` | Run `make init` ([First start](#first-start)) |
| `migrate` exits 1 and `api` and `worker` never start | `APP_ADMIN_PASSWORD_HASH` is unset or not an argon2 hash ("APP_ADMIN_PASSWORD_HASH is not set; run `make init ADMIN_PASSWORD=…`"), or the database is unreachable | `docker compose logs migrate`; write a new hash ([Admin login](#admin-login)) |
| `api` restarts with "APP_BASE_URL must use https:// unless it is a localhost URL" | A non-local `http://` base URL | Use `https://` with TLS in front of nginx ([Hosting beyond localhost](#hosting-beyond-localhost)), or a `localhost` URL |
| `api` restarts with "SESSION_SECRET is not set; run make init" | `SESSION_SECRET` blank | Generate one ([Secrets](#secrets)) and recreate `api` |
| Sign-in answers 429 | More than 5 attempts in a minute from one address | Wait a minute |
| `/actions` shows "No analysis run yet" for more than 15 minutes | The first build, backfill or run failed, or the worker is restarting | `docker compose logs -f worker`; a configuration error appears as `worker_config_invalid` |
| A source stays simulated after its credentials were added | Variables missing ("credentials not configured"), a denied token or scope (the reason names the code and endpoint), partial permissions, or the probe interval has not passed | Read the reason on `/sources`, fix the access, recreate `api` and `worker` and allow up to 30 minutes for the next probe. Partial success, such as LinkedIn ads readable while Lead Sync returns 403, changes nothing by design |
| LinkedIn stays simulated although its probe passed | The live build's posts read was denied ([LinkedIn token bootstrap](#linkedin-token-bootstrap)) | Grant post read access, or accept simulated LinkedIn data |
| Datasets are stale | Transient errors (429, 5xx, timeouts, `RESOURCE_EXHAUSTED`) or the Google call budget exhausted | Nothing to do at first: the last good data is kept and retries are automatic. Deferred Google pages resume in a later 24 h window. Check the call count on `/sources` |
| Google stays stale while other n8n workflows run normally | n8n answers an invalid query with HTTP 500 "Error in workflow", which is indistinguishable from a transient 5xx | Find the failing query id in `docker compose logs worker`, and the execution in n8n's Executions tab |
| A job failed with a defect | HTTP 400, GAQL `QueryError` or a validation error. Never retried | `docker compose logs worker`: the structured JSON names the job and the query id |
| A dataset is incomplete | A Google search-term partition (Q-G6) returned 10,000 rows at its finest split, a HubSpot deal-search hour exceeded 10,000 results, or pages were deferred | Affected items carry `incomplete`. Deferred work completes later; the two platform limits stay flagged |
| A discrepancy banner sits above everything | A platform status or budget differs from the last recorded decision | Resolve it on `/decisions`. The valid resolutions per kind are in [C0 resolutions](recommendation-rules.md#c0-resolutions) |
| MQL or QL shows "unavailable" | The stage label in `config/hubspot_mapping.yaml` is not among the portal's `lifecyclestage` options, or its date property is missing | Read the reason on `/settings`. Fix the label, or bind a date property with `mode: property`, then restart `api` and `worker` |
| The worker exits at startup with `worker_config_invalid` | A configuration rule failed, for example `window.presets` lacks `window.default_days`, or a lead form or creative is listed under two assets | Fix the YAML named in the log, then `docker compose restart worker api` |
| A card shows "Computed from replaced data", or an action answers 409 `subject_retired` | The item was computed from a dataset that has since been replaced | Wait for the rerun the activation queued, then reload |
| `ads-engine analyze` exits 3, or a custom window answers 409 `run_in_progress` | Another run holds the shared analysis lock | Wait and retry |
| A service keeps restarting under load, or `migrate` shows "Exited (137)" in `docker compose ps --all` | The service exceeded its memory ceiling and was killed. `docker events --since 1h --until 0s --filter event=oom` names each killed container. `docker inspect --format '{{.State.OOMKilled}}' <container>` prints `true` for `migrate`, but for a restarted service only until Docker starts it again | Raise that service's `<SERVICE>_MEMORY_LIMIT` in `.env`, for example `WORKER_MEMORY_LIMIT=3g` ([Resource limits](#resource-limits)), then run `docker compose up -d` |
| Port 8080 is already in use | Another service on the host | Publish another host port with a `docker-compose.override.yml` as in [Hosting beyond localhost](#hosting-beyond-localhost), for example `"127.0.0.1:8081:8080"`, set `APP_BASE_URL=http://localhost:8081`, and run `make up APP_URL=http://localhost:8081` |

## Operating commands

The `ads-engine` CLI is on `PATH` in the backend image, whose working directory is `/app`, where
`alembic.ini` and the mounted `config/` live. Run it inside the running `worker` container with
`docker compose exec`, or through `make` where a target exists.

```bash
# Upgrade the schema and refresh the admin login (one-shot migrate service)
make migrate

# Run one source's sync jobs now and print the JSON report (google, linkedin or hubspot)
docker compose exec worker ads-engine sync --source linkedin

# Compute a custom-window run from stored data
docker compose exec worker ads-engine analyze --start 2026-09-01 --end 2026-09-28

# Follow the structured JSON logs
docker compose logs -f worker api

# Container state, one-shot migrate included
docker compose ps --all

# Liveness and database check through nginx: {"status": "ok"}
curl -fsS http://localhost:8080/api/v1/healthz
```

- **`sync`** runs the source's jobs against its active dataset, live or simulated. It exits 0 only
  when every job ran and succeeded, and 1 when no job ran (no active dataset yet, or a live dataset
  that may not be synced now) or any job failed; the report says which. "Sync now" on `/sources`
  queues the same work for the worker instead.
- **`analyze`** takes calendar dates in `YYYY-MM-DD` form, applied to each source's native days.
  `--end` is no later than the default window end, the earlier of yesterday in the Google account's
  time zone and yesterday UTC, and the span is 1 to `window.max_days` (default 180) days. The run is
  `kind=custom`; open it at `/actions?run=<run_id>` with the `run_id` from its JSON summary.
- **Streams**: stderr carries JSON log lines, including one `cli_error` line that explains a
  failure, and never a secret, a token or an email. stdout carries only command output: the JSON
  reports of `sync`, `analyze` and `seed-simulated` and the paths `capture-fixtures` writes, none
  of which carries a secret, a token or an email. The exception is `hash-password`, whose stdout
  is the argon2id password hash, the secret value of `APP_ADMIN_PASSWORD_HASH`. Capture it
  straight into `.env` by command substitution, as `make init` and the [Admin login](#admin-login)
  block do; never echo it, log it, paste it into a ticket or leave it in shell history or a CI log.

Exit codes of every `ads-engine` command:

| Code | Meaning | What to do |
|------|---------|------------|
| 0 | Success | Nothing |
| 1 | Failure: the command failed or did no work; its `cli_error` line says why | Read the message and the log lines |
| 2 | Usage error: a bad argument, a date not in `YYYY-MM-DD` form, a window out of range, or no `alembic.ini` or contracts directory in the working directory | Fix the arguments, or run from `/app` in the image (`backend/` on the host) |
| 3 | Another process holds the shared analysis lock (`analyze`, `seed-simulated`) | Wait and retry. `POST /api/v1/runs` answers 409 `run_in_progress` in the same situation |

## Capturing live contract fixtures

`ads-engine capture-fixtures --source <google|linkedin|hubspot>` attempts every contract surface of
the source. For each surface that had data to read, it records one redacted live request and
response into `backend/tests/contracts/<source>/captured/<surface>.json`, shaped
`{surface, captured_at, request, response}`. The contract tests then validate the parsers against
those captures as well as the documented samples. A surface without data is skipped with a
warning, so an exit of 0 does not mean every surface was captured: read the `cli_warning` lines
that say "not captured", or the `skipped` count in the `capture_fixtures_finished` log line,
before relying on the captures.

It runs only when that source's credentials work, and it is refused, with nothing sent or written,
under `DATA_MODE=simulated`, without the source's credentials, or when the output directory is not
writable or is a symbolic link. Google captures reserve every call in the shared call budget in the
database, so they count against `GOOGLE_DAILY_CALL_BUDGET` like the worker's calls.

With the stack running and `DATA_MODE=auto`, run it in a one-off `worker` container that writes
straight into the checkout:

```bash
SOURCE=linkedin   # google, linkedin or hubspot
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$PWD/backend/tests/contracts/$SOURCE/captured:/app/tests/contracts/$SOURCE/captured" \
  worker ads-engine capture-fixtures --source "$SOURCE"
```

- `--no-deps` uses the running `db`, which Google captures need for the call budget, without
  re-running `migrate`. The container receives `.env`, so the credentials come from there.
- `--user "$(id -u):$(id -g)"` runs the command as the owner of the checkout. The image's runtime
  user (UID 10001) owns only the image's own `captured/` directories, so without it a bind-mounted
  directory owned by another user is refused as "not writable by this user (uid 10001)".
- Each written path is printed on stdout. Each skipped surface gets one `cli_warning` line whose
  `message` reads `ads-engine: capture-fixtures <source>: not captured: <reason>`, the reason
  naming the surface or query and why, such as no sponsored LinkedIn lead to re-fetch or a Google
  query that answered with no rows. Exit 1 means a call failed, nothing was captured, a capture
  broke its contract, or a capture still held a configured secret after redaction; that capture is
  not written.
- Redaction replaces tokens, secrets, emails, names and form answers with `"<redacted>"`, ad
  account and customer ids with zeros of the same length, and the webhook URL with a reserved
  placeholder URL.

Review the captures before committing them. Scan the working tree with the pinned gitleaks image
first, then commit, then run `make secret-scan` over the history before pushing:

```bash
git status --short backend/tests/contracts
git diff --stat -- backend/tests/contracts
docker run --rm -v "$PWD:/repo:ro" -w /repo zricethezav/gitleaks:v8.30.1 \
  dir backend/tests/contracts --config /repo/.gitleaks.toml --redact --exit-code 1
```

## Changing thresholds, mappings and the registry

The three files in `config/` are bind-mounted read-only at `/app/config` in `migrate`, `api` and
`worker`, so an edit needs no rebuild:

- `config/thresholds.yaml`: every rule threshold, plus the window settings `window.default_days`
  (default 28), `window.trusted_min_days` (default 14), `window.presets` (default
  `[14, 28, 56, 90]`) and `window.max_days` (default 180), and `sources.stale_after_hours`
  (default 24).
- `config/hubspot_mapping.yaml`: the MQL and QL bindings, `crm.booking_sources` and
  `crm.meeting_outcome_exclusions`.
- `config/content_assets.yaml`: the white paper registry, mapping each asset to its canonical
  LinkedIn lead-form URNs and optional creative overrides.

After an edit, restart both processes:

```bash
docker compose restart worker api
```

- The API reads the configuration once per process; the worker reloads it at startup, replaces the
  white paper registry tables and re-verifies the HubSpot bindings.
- The worker compares the new `config_hash` with the latest default run's. When it changed, it
  queues a default-window run, which applies the new values. Editing only comments leaves the hash
  unchanged.
- An invalid file stops the worker with `worker_config_invalid`, naming the file and the key
  ([Troubleshooting](#troubleshooting)).
- `/settings` shows the values in force, the resolved HubSpot bindings and `config_hash`.

Each threshold, its rationale and the load-time checks are in
[recommendation rules](recommendation-rules.md#tuning-thresholds).

## Hosting beyond localhost

The local stack serves plain HTTP on port 8080 and accepts an `http://` base URL only for
`localhost` or `127.0.0.1`. Any other host needs TLS in front of nginx:

1. **Terminate TLS** in a reverse proxy or load balancer in front of nginx's port 8080. Forward
   `X-Forwarded-For` and `X-Forwarded-Proto` to it.
2. **Set the public origin** in `.env`:

   ```dotenv
   APP_BASE_URL=https://<host>
   ```

   With `https://`, the session cookie is marked Secure. The API refuses to start on any `http://`
   base URL other than `localhost` or `127.0.0.1`. Apply it with
   `docker compose up -d --force-recreate api worker`.
3. **Expose only nginx, and only to the proxy**. Compose publishes `8080:8080` on every interface;
   a host-local `docker-compose.override.yml` beside `docker-compose.yml`, which Compose reads
   automatically, restricts it. Keep that file out of commits:

   ```yaml
   services:
     web:
       ports: !override
         - "127.0.0.1:8080:8080"
   ```

   Never publish `db` (5432) or `api` (8000). The API trusts the client address in
   `X-Forwarded-For` from every peer, which is safe only while nginx is its sole client-facing peer.
4. **Keep the sign-in limit per client**. nginx overwrites `X-Forwarded-For` with the address of the
   peer connected to it, which behind a TLS proxy is the proxy itself, so every client would share
   one limit. Trust the proxy with nginx's realip module in the `server` block of `web/nginx.conf`,
   then rebuild `web`:

   ```nginx
   set_real_ip_from <proxy address or CIDR>;
   real_ip_header X-Forwarded-For;
   real_ip_recursive on;
   ```

   ```bash
   docker compose up -d --build web
   ```

5. **Change the database password** from the local default `ads`. `POSTGRES_PASSWORD` takes effect
   only when the `pgdata` volume is first initialised, so set it, together with the password inside
   `DATABASE_URL` (percent-encoded where it holds special characters), before the first
   `docker compose up`. On an existing volume, change the role's password first; `\password`
   prompts for it without echo:

   ```bash
   docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
   # at the psql prompt: \password <POSTGRES_USER value>, then \q
   ```

   Then update `POSTGRES_PASSWORD` and `DATABASE_URL` in `.env` and apply them with
   `docker compose up -d`.

## Database reset and backup

All state lives in the named volume `pgdata`: datasets, snapshots, decisions, discrepancy
resolutions, feedback, analysis runs and the admin login. `make down` (`docker compose down`) keeps
it.

Back it up first, from the repository root, to a file outside the checkout. `mktemp` creates a new
file with a unique name in your home directory, readable and writable only by you (mode 600)
before `pg_dump` writes a byte, and never reuses an existing file. The block removes the file if
the dump fails and lists it when the dump succeeds:

```bash
if dump_file="$(mktemp "$HOME/ads_engine.dump.XXXXXX")"; then
  if (umask 077 && docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$dump_file"); then
    ls -l -- "$dump_file"
  else
    rm -f -- "$dump_file"
    echo "pg_dump failed; no dump kept" >&2
  fi
else
  echo "mktemp failed; nothing written" >&2
fi
```

The dump holds the admin email and password hash and the private CRM, pipeline and decision
data. Keep it owner-only (mode 600) outside the checkout, never commit or share it, copy it only
to storage with the same protection, and delete it when you no longer need it.

A full reset deletes the volume and everything stored in it. **Every recorded decision, resolution
and feedback entry is lost**, and so is the history the product accumulated: the dated status,
budget and quality-score snapshots, and all data older than the backfill. The rebuild backfills
only `BACKFILL_DAYS`, capped at 180 days. Older data may still exist at the source, depending on
its retention (LinkedIn keeps daily analytics for 6 months, Google keeps granular reporting data
for 37 months), but the product never reads it back, so a dump is the only way to keep the older
history:

```bash
docker compose down -v
docker compose up -d --build
```

On the next start, `migrate` recreates the schema and the admin login from `.env`, and the worker
builds and backfills a new dataset per source, live or simulated, writes fresh baseline decisions
and computes the first default run.

## Tests and quality checks

These targets run tools on the host, so they need uv, Python and Node in addition to Docker
([Prerequisites](#prerequisites)).

- **`make test`**: starts a disposable `postgres:18.6` container of its own, named
  `ads-engine-testdb-<random token>`, on `127.0.0.1:TEST_DB_PORT` (default 55432;
  `TEST_DB_PORT=0` lets Docker pick a free port, so several checkouts can run it at once) and
  points `TEST_DATABASE_URL` at it. It runs the backend tests (`uv sync --frozen`, then
  `uv run --frozen pytest`) and the web unit tests (`npm ci`, then `npm test`). On exit, Ctrl-C
  included, it removes that container and its volume and checks for up to about 10 s that they
  are gone. A port already in use fails the run. It never reuses or removes a container it did not
  create, and a removal it cannot confirm fails the run, even when the tests passed, printing the
  `docker` command that finds or removes what is left. It never touches the stack's database.

  ```bash
  make test TEST_DB_PORT=55433 PYTEST_ARGS=tests/unit
  make test TEST_DB_PORT=0
  ```

- **`make lint`**: `ruff format --check`, `ruff check` and `mypy --strict src` for the backend;
  `npm run lint` (ESLint and stylelint) and `npm run typecheck` for the web client.
- **`make e2e ADMIN_PASSWORD=<value>`**: starts the stack with `DATA_MODE=simulated`, waits for
  health, installs the Playwright browser (`PLAYWRIGHT_INSTALL`; set it empty to skip) and runs
  Playwright with `E2E_BASE_URL`, `E2E_ADMIN_EMAIL` and `E2E_ADMIN_PASSWORD` derived from
  `APP_URL`, `ADMIN_EMAIL` and `ADMIN_PASSWORD`. It leaves the stack running in simulated mode; run
  `docker compose up -d` to return to the `.env` mode. Against an already running stack, run
  Playwright directly; `E2E_ADMIN_PASSWORD` is required, while `E2E_BASE_URL`
  (`http://localhost:8080`) and `E2E_ADMIN_EMAIL` (`admin@example.com`) have defaults:

  ```bash
  read -rs -p 'Admin password: ' E2E_ADMIN_PASSWORD; echo
  (cd web && E2E_ADMIN_PASSWORD="$E2E_ADMIN_PASSWORD" npm run e2e)
  unset E2E_ADMIN_PASSWORD
  ```

- **`make secret-scan`**: gitleaks over every commit ([Secrets](#secrets)).

## Make targets

`make` with no target, or `make help`, lists them. Every recipe runs as one bash script that stops
at the first failing command.

| Target | What it does |
|--------|--------------|
| `help` | Lists the targets (the default goal) |
| `check-tools` | Verifies Docker Compose `>=5.5.1,<6` and Docker Engine `>=29`, and prints the found versions and the required range |
| `init` | `check-tools`; requires `ADMIN_PASSWORD`; refuses an existing `.env`; copies `.env.example` to `.env` (mode 600); writes a random `SESSION_SECRET`; builds the backend image (`docker compose build migrate`) and writes the single-quoted argon2id `APP_ADMIN_PASSWORD_HASH` from `ads-engine hash-password`; removes the partial `.env` on failure |
| `up` | `check-tools`; requires `.env`; `docker compose up -d --build`; waits up to `HEALTH_TIMEOUT` (default 180) seconds for `APP_URL/api/v1/healthz` (default `http://localhost:8080`) to answer 200, and prints the containers' state and recent logs when it does not |
| `down` | `docker compose down`: stops and removes the containers and keeps the `pgdata` volume |
| `migrate` | Requires `.env`; `docker compose run --rm migrate`, which runs `ads-engine migrate && ads-engine ensure-admin` |
| `seed-simulated` | Requires `.env`; `docker compose run --rm migrate sh -c 'ads-engine migrate && ads-engine seed-simulated'` |
| `test` | Backend pytest on a disposable `postgres:18.6` of its own (`ads-engine-testdb-<random token>` on `127.0.0.1:TEST_DB_PORT`, default 55432, `0` for a free port; a container it did not create is never reused or removed), then the web unit tests. On exit, Ctrl-C included, it removes that container and its anonymous volume and checks for up to about 10 s that they are gone; a removal it cannot confirm fails the target, even when the tests passed, and prints the `docker` command that finds or removes what remains. `PYTEST_ARGS` passes extra pytest arguments |
| `lint` | `uv sync --frozen`, then `ruff format --check`, `ruff check`, `mypy --strict src`; `npm ci`, then `npm run lint`, `npm run typecheck` |
| `e2e` | `check-tools`; requires `.env` and `ADMIN_PASSWORD`; starts the stack with `DATA_MODE=simulated` as `up` does; `npm ci`; `PLAYWRIGHT_INSTALL`; `npm run e2e`; leaves the stack running |
| `secret-scan` | `zricethezav/gitleaks:v8.30.1` with `.gitleaks.toml` over every commit (`gitleaks git --redact --exit-code 1`); a finding fails it |

Overridable variables, on the command line or in the environment: `COMPOSE` (default
`docker compose`; add `-p` or `-f` flags here), `APP_URL`, `HEALTH_TIMEOUT`, `TEST_DB_PORT`,
`PYTEST_ARGS`, `PLAYWRIGHT_INSTALL`, `ADMIN_EMAIL` and `ADMIN_PASSWORD`.
