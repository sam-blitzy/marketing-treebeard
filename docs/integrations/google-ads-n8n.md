# Google Ads through the n8n webhook

The Ads Action Engine reaches Google Ads only through the existing n8n Cloud webhook. Direct OAuth is not available for this account, because the registered app's redirect URI is locked to n8n's own callback. The product therefore only calls the workflow: changing the workflow or its response, including adding a page token, is out of scope. No Google client ID, client secret, refresh token or developer token exists anywhere in the product; they stay in n8n's credential store, and there is no Google OAuth flow, consent screen or token handling. The product is read-only towards Google Ads: every query is a GAQL `SELECT`, and no mutate operation is ever sent.

This page is for an operator wiring the n8n workflow and for a developer changing the Google connector. It describes what the code does. The connector lives in `backend/src/ads_engine/connectors/google_n8n/` (`client.py`, `queries.py`, `parser.py`, `partition.py`); the jobs that call it are in `backend/src/ads_engine/sync/`. For the wider system see [the architecture overview](../architecture.md), for the rules that consume the data see [the recommendation rules](../recommendation-rules.md), and for setup see [the runbook](../runbook.md) and [the README](../../README.md).

## Configuration

Every value is a server-side environment variable read by `backend/src/ads_engine/settings.py` and listed in the root `.env.example`. A blank value counts as unset, so the default applies.

| Variable | Required for live data | Default | Purpose |
|----------|------------------------|---------|---------|
| `N8N_GOOGLE_ADS_WEBHOOK_URL` | Yes | None (empty in `.env.example`) | The n8n webhook that relays `googleAds:search` v25. Must be `https://`; plain `http://` is accepted only on `localhost` or `127.0.0.1`. The URL identifies the workflow, so it is never logged; logs keep its host at most. |
| `N8N_WEBHOOK_SECRET_HEADER` | No | `X-Webhook-Secret` in code when blank or unset; `.env.example` ships `X-Webhook-Token` | Name of the header that carries the secret. It must equal the Name on the n8n Header Auth credential. The existing credential expects `X-Webhook-Token` and refuses `X-Webhook-Secret` with 403, which the product reads as access denied; that is why `.env.example` sets it. It must be an RFC 7230 token and cannot be `Content-Type`, `Content-Length`, `Transfer-Encoding`, `Host` or `Connection`. |
| `N8N_WEBHOOK_SECRET` | Yes | None (empty in `.env.example`) | The header value. Held as a pydantic `SecretStr`, so it never prints; it is never logged and never reaches the browser bundle. It must be visible ASCII with no surrounding whitespace. |
| `GOOGLE_ADS_CUSTOMER_ID` | Yes | None (empty in `.env.example`) | The Google Ads customer id. Dashes are removed at load, and the result must be exactly 10 digits; it is sent with no dashes. |
| `GOOGLE_DAILY_CALL_BUDGET` | No | `1000` | The product's own cap on webhook calls per sliding 24 h (see [Call budget and quotas](#call-budget-and-quotas)). At least 1. |
| `DATA_MODE` | No | `auto` | `auto` uses live data when credentials work and falls back to simulated data otherwise. `simulated` forces simulated data for every source and never probes. |
| `BACKFILL_DAYS` | No | `180` | Length of the initial backfill of every new dataset, in native days. |
| `DISPLAY_TIMEZONE` | No | `UTC` | Wall-clock zone of the sync schedule and of displayed instants. It never moves Google's day boundary. |

When `N8N_GOOGLE_ADS_WEBHOOK_URL`, `N8N_WEBHOOK_SECRET` or `GOOGLE_ADS_CUSTOMER_ID` is missing, Google runs on simulated data with the reason "credentials not configured". This is not an error.

n8n's own guidance is to protect a webhook with Header Auth stored as an n8n credential; a request without the header is rejected with 401 before the workflow runs. The setup steps are in [the runbook](../runbook.md).

## Request contract

Each webhook call is one `POST` carrying one GAQL query:

```http
POST <N8N_GOOGLE_ADS_WEBHOOK_URL>
<N8N_WEBHOOK_SECRET_HEADER>: <N8N_WEBHOOK_SECRET>
Content-Type: application/json
```

The body is exactly:

```json
{"customer_id": "<10 digits, no dashes>", "query": "<GAQL>", "page_token": null}
```

- **Three keys, in this order, and no others.** No client ID, client secret, refresh token, developer token or `login-customer-id` is ever sent. The body is serialised once per call, so every retry sends the same bytes.
- **`page_token`** is `null` on a first page. On a follow-up page it carries the previous response's `nextPageToken`.
- **One GAQL query per call.** The workflow's Google Ads node runs it as `googleAds:search` v25, the REST call `POST https://googleads.googleapis.com/v25/customers/<customer id>/googleAds:search`. `searchStream` is never used. `pageSize`, `validateOnly` and `searchSettings` are never sent.
- **Every query is a template** from `QUERIES` in `queries.py` (see [Queries](#queries)). Scope values are validated integer ids and dates only, so no caller-supplied text is interpolated into GAQL.
- **Timeout**: 120 s per call (`SOURCE_TIMEOUTS["google"]` in `connectors/http.py`), with a 10 s connect timeout.

## Response contract

n8n answers synchronously with the `SearchGoogleAdsResponse` as `{results[], fieldMask, queryResourceConsumption}`. A Q-G1 response, with obviously fake values, looks like this:

```json
{
  "results": [
    {
      "customer": {
        "resourceName": "customers/0000000000",
        "id": "0000000000",
        "descriptiveName": "Example account",
        "currencyCode": "USD",
        "timeZone": "America/New_York"
      }
    }
  ],
  "fieldMask": "customer.id,customer.descriptiveName,customer.currencyCode,customer.timeZone",
  "queryResourceConsumption": "10"
}
```

- **Rows** are REST `GoogleAdsRow` objects with camelCase keys, nested per resource. INT64 values (ids, `*Micros`, `impressions`, `clicks`, `historicalQualityScore`) arrive as strings. Zero-valued and unset fields may be omitted.
- **`queryResourceConsumption`** is accepted as an int64 string or a JSON integer. It is logged only and is not the quota.
- **n8n's "all incoming items" shape**, a one-element JSON array wrapping that object, is also accepted and unwrapped before validation.

The client (`client.py`) validates every envelope before any row is used:

| Response | Treated as |
|----------|-----------|
| `fieldMask` present and `results` absent | Zero rows. Google omits an empty repeated field |
| Neither `results` nor `fieldMask` | `QueryDefect`, never an empty page, because an empty Q-G2 would feed false `missing` inferences |
| `fieldMask` that does not list exactly the selected fields, in any order | `QueryDefect`, because an omitted metric would otherwise read as zero |
| More than 10,000 rows, `results` not a list of objects, or a non-string `fieldMask` or `nextPageToken` | `QueryDefect` |
| `nextPageToken` present | Sent back at once as `page_token` |
| A page token returned twice in one query, or more than 1,000 pages | `QueryDefect` |

**Pagination.** `googleAds:search` returns fixed pages of 10,000 rows (`PAGE_SIZE`). The response the user specified carries no page token, and the product does not depend on one. When a response does carry `nextPageToken`, the client follows it immediately, because page tokens expire after a few hours. A response with fewer than 10,000 rows is complete; how the product handles a response of exactly 10,000 rows is described in [Completeness](#completeness).

**Parsing** (`parser.py`). Each row is checked against its `row_*.json` contract and turned into a typed row:

- **Money**: `metrics.cost_micros`, `campaign_budget.amount_micros` and `campaign_budget.total_amount_micros` are divided by 1,000,000 exactly into decimal amounts in the account currency (`customer.currency_code`). `metrics.average_cpc`, also in micros, is rounded half-even to the micro. Budget micros are also kept as integers, so the discrepancy check (C0) compares budgets at native precision.
- **Budget amount by period**: a `DAILY` budget's amount is in `amount_micros` and a `CUSTOM_PERIOD` (lifetime) budget's in `total_amount_micros`. The two are mutually exclusive. An absent period reads as Google's default, `DAILY`.
- **Conversions**: `metrics.conversions` and `metrics.conversions_value` stay decimals and may be fractional, under data-driven attribution or `MANY_PER_CLICK` counting. They are never rounded to whole numbers. [The recommendation rules](../recommendation-rules.md) describe how conversions are used.
- **Status**: campaigns, ad groups and keywords map `ENABLED` → `active`, `PAUSED` → `paused` and `REMOVED` → `ended`. The raw status is kept beside the normalised one, and any other value is a defect.
- **Missing values**: absent additive metrics (`costMicros`, `clicks`, `impressions`, `conversions`, `conversionsValue`) are zero, because REST JSON omits zero-valued fields. Absent rate and score values (`historicalQualityScore`, `qualityInfo.qualityScore`, `averageCpc`, the impression shares) and absent budget amounts are unavailable (`None`), never zero. An explicit JSON `null` is a defect.
- **Dates**: `segments.date` is kept as written (see [Day boundaries](#day-boundaries-and-search-term-coverage)).

## Queries

`QUERIES` in `queries.py` holds every read the product makes. The fields and filters below are copied from it, and each `SELECT` list equals `request.gaql.select` in the query's row contract. `{last_id}` is the keyset position, `{start}` and `{end}` the native date range, and the id ranges are the narrowing forms described in [Completeness](#completeness).

| ID | Resource | Fields and filter | Strategy | Used by |
|----|----------|-------------------|----------|---------|
| Q-G1 | `customer` | `customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone`; no filter | Single call | The Google probe, the account currency, and the account time zone that sets Google's native day |
| Q-G2 | `campaign` | `campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, campaign_budget.id, campaign_budget.amount_micros, campaign_budget.total_amount_micros, campaign_budget.period, campaign_budget.explicitly_shared`; `WHERE campaign.status != 'REMOVED' AND campaign.id > {last_id} ORDER BY campaign.id LIMIT 9999` | Keyset scan on `campaign.id` | Campaign and budget snapshots, shared-budget membership, the discrepancy check (C0), utilization |
| Q-G2b | `campaign` | Q-G2's fields; `WHERE campaign.id IN ({ids})` with at most 1,000 ids and no status filter | By ids | Tracked campaigns a complete Q-G2 scan no longer returned: one returned as `REMOVED` is snapshotted as `ended`, one not returned at all raises a `missing` discrepancy |
| Q-G3 | `campaign` | `campaign.id, segments.date, metrics.cost_micros, metrics.impressions, metrics.clicks, metrics.conversions, metrics.conversions_value, metrics.search_budget_lost_impression_share, metrics.search_impression_share`; `WHERE segments.date BETWEEN '{start}' AND '{end}'` | Dated | Pause, scale, budget and weekday/weekend rules (C1, C2, B1–B4) |
| Q-G4 | `ad_group` | `ad_group.id, ad_group.name, ad_group.status, campaign.id`; `WHERE ad_group.status != 'REMOVED' AND ad_group.id > {last_id} ORDER BY ad_group.id LIMIT 9999` | Keyset scan on `ad_group.id` | Keyword context and the keyword-cluster gap (C3b) |
| Q-G5 | `keyword_view` | `ad_group_criterion.criterion_id, ad_group.id, segments.date, metrics.historical_quality_score, metrics.average_cpc, metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions`; `WHERE segments.date BETWEEN '{start}' AND '{end}'` | Dated, then a criterion keyset in one (day, ad group) | Per-day keyword trends (K3) and search-term coverage |
| Q-G6 | `search_term_view` | `search_term_view.search_term, search_term_view.status, segments.keyword.ad_group_criterion, segments.keyword.info.text, segments.keyword.info.match_type, ad_group.id, campaign.id, segments.date, metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value`; `WHERE segments.date BETWEEN '{start}' AND '{end}'` | Dated | Negative-keyword candidates (K1), expansion candidates (K2), keyword clusters (C3b) |
| Q-G7 | `ad_group_criterion` | `ad_group_criterion.criterion_id, ad_group.id, campaign.id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.status, ad_group_criterion.quality_info.quality_score`; `WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED' AND ad_group.id >= {lo} AND ad_group.id <= {hi}` | `ad_group.id` ranges, then a criterion keyset in one ad group | The current keyword list. Each entity sync writes keyword rows and one quality-score snapshot per keyword, including keywords with no traffic |
| Q-G8 | `conversion_action` | `conversion_action.id, conversion_action.name, conversion_action.status, conversion_action.include_in_conversions_metric, conversion_action.counting_type, conversion_action.attribution_model_settings.attribution_model`; no filter | Single call | The conversion counting contract (B4). One snapshot per action per entity sync |

Two fields go beyond the plan's query list. Both are in `queries.py` and in the row contracts:

- **`campaign_budget.total_amount_micros` in Q-G2 and Q-G2b.** Google stores a `CUSTOM_PERIOD` budget's amount there and leaves `amount_micros` unset. The `own_budget` decision's `total_budget` field and the C0 comparison need that platform value.
- **`campaign.id` in Q-G3.** Each daily row must be keyed to its campaign.

Status filters follow one rule:

- **Entity queries** Q-G2, Q-G4 and Q-G7 exclude `REMOVED` rows.
- **Q-G2b** re-reads, with no status filter, the tracked campaigns a complete Q-G2 scan no longer returned. A removal therefore reaches the discrepancy check, but only from complete reads (see [Safe absence](#safe-absence)).
- **Metrics queries** Q-G3, Q-G5 and Q-G6 are bounded only by `segments.date BETWEEN` the fetch range and carry no status filter, so spend by a campaign removed during the window still counts. When a row names a campaign or ad group that no entity sync stored, a stand-in entity is created so its spend is kept.
- **Q-G8** has no status filter, because a removed or hidden conversion action may still have contributed to past days. It is the counting contract that B4 reads to decide whether Google conversions are unweighted event counts; see [the recommendation rules](../recommendation-rules.md).

## Sync schedule

Slots are wall-clock times in `DISPLAY_TIMEZONE`. Daily facts are upserted per native day, so a later read overwrites earlier values and restated data wins. The full schedule, including LinkedIn and HubSpot, is in [the architecture overview](../architecture.md).

| Job | Cadence | Queries | Range | Freshness dataset |
|-----|---------|---------|-------|-------------------|
| Entity sync | Every 3 h (00:00, 03:00, 06:00, …) | Q-G1, Q-G2 then Q-G2b, Q-G4, Q-G7, Q-G8 | Full current state | `google.entities` |
| Metrics, incremental | Every 6 h (00:00, 06:00, 12:00, 18:00) | Q-G3, Q-G5, Q-G6 | Trailing 3 native days, ending today in the account time zone | `google.metrics_daily` (Q-G3, Q-G5), `google.search_terms` (Q-G6) |
| Metrics, restatement | Nightly at 03:00 | Q-G3, Q-G5, Q-G6 | Trailing 30 native days, to absorb conversion lag | As above |
| Initial backfill | Every new dataset, including the first start | Entity sync, then Q-G3, Q-G5, Q-G6 | `BACKFILL_DAYS` (default 180) | As above |
| Recovery probe | Every 30 min while Google is in automatic fallback | Q-G1 | — | — |

When a dataset still has deferred pages from an earlier run, the next metrics run widens its start back to the dataset's `covered_from`, so the shortfall is re-read.

## Completeness

A response with fewer than 10,000 rows is complete. A response of exactly 10,000 rows with no page token has unknown completeness: it may be truncated, and a truncated response cannot reveal the ids it omitted. No step therefore relies on an id list read off a capped response. `partition.py` reads every template to completion by its strategy, using two GAQL features: numeric comparisons on INT64 ids, and `ORDER BY … LIMIT`. Ids range over `[0, INT64_MAX]`, where `INT64_MAX` is 9223372036854775807.

**Keyset scans (Q-G2, Q-G4).** These queries always run as keyset scans. The rendered Q-G2 text, on one line as the code sends it, is:

```sql
SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, campaign_budget.id, campaign_budget.amount_micros, campaign_budget.total_amount_micros, campaign_budget.period, campaign_budget.explicitly_shared FROM campaign WHERE campaign.status != 'REMOVED' AND campaign.id > {last_id} ORDER BY campaign.id LIMIT 9999
```

- Every page is limited to 9,999 rows (`KEYSET_LIMIT`), below the 10,000-row page size, so no page can be silently truncated.
- The first page uses `last_id` 0. While a page returns exactly 9,999 rows, the scan continues from that page's largest id; the first shorter page ends it.
- Ids strictly increase, so the scan terminates, and its union is every non-removed campaign (Q-G2) or ad group (Q-G4, keyed on `ad_group.id`).
- Worked example: 25,000 campaigns are read as pages of 9,999, 9,999 and 5,002 rows, with filters `campaign.id > 0`, `campaign.id > <largest id of page 1>` and `campaign.id > <largest id of page 2>`. The plan quotes a last page of 7,002 rows, which would take 27,000 campaigns; the integration tests prove both cases.
- A keyset page with more than 9,999 rows, one that does not advance, or one that repeats an id is a `QueryDefect`.

**Q-G2b.** The ids are sorted, de-duplicated and sent at most 1,000 per `IN` list (`MAX_IDS_PER_IN`). Q-G2b returns at most one row per campaign, so a successful chunk is always complete. An empty id set sends no call.

**Q-G7.** `ad_group_criterion.criterion_id` is unique only within its ad group, so keywords are read in two steps:

- `ad_group.id` ranges, starting from `[0, INT64_MAX]` (`ad_group.id >= {lo} AND ad_group.id <= {hi}`). A range that returns 10,000 rows is halved.
- A single ad group that still returns 10,000 rows is read by keyset on the criterion: `ad_group.id = {ad_group_id} AND ad_group_criterion.criterion_id > {last_id} ORDER BY ad_group_criterion.criterion_id LIMIT 9999`. A 15,000-keyword ad group is read as pages of 9,999 and 5,001 rows.

Both steps narrow a finite integer range, so they terminate with every keyword read.

**Dated queries (Q-G3, Q-G5, Q-G6).** A capped partition splits the first dimension that is not yet single, in this order:

1. The date range, halved down to single days.
2. At one day, a numeric `campaign.id` range (`campaign.id >= {lo} AND campaign.id <= {hi}`), halved from the full INT64 range down to one campaign. The ranges are numeric, never lists of known ids, so a campaign removed during the window is still covered.
3. At one day and one campaign, a numeric `ad_group.id` range, halved down to one ad group. The campaign filter stays.

At the finest partition each query ends differently:

| Query | At its finest partition |
|-------|-------------------------|
| Q-G3 | One row per campaign per day, so it cannot reach 10,000 rows at one campaign-day. A capped campaign-day breaks that contract and is a `QueryDefect` |
| Q-G5 | One (day, ad group) still at 10,000 rows is read by keyset on `ad_group_criterion.criterion_id` with `LIMIT 9999` |
| Q-G6 | Search-term rows have no unique sortable id. One (day, ad group) still at 10,000 rows is stored as returned, and `google.search_terms` is marked `incomplete` with the reason "result limit reached at finest partition", naming up to three (day, ad group) partitions. Every item that reads it carries the `incomplete` flag |

Q-G1 and Q-G8 are single calls with nothing to partition by. A capped response would be kept and marked incomplete ("result limit reached"), although neither query comes near 10,000 rows in practice. Apart from these two platform limits and call-budget deferral (below), every partitioned read ends complete.

Partitioning is deterministic: depth first, dates ascending, then ranges ascending. The rows of a capped response that is split are discarded, and its two halves re-read them. Every accepted row is checked against the partition it was read for, so a relay that ignored a filter fails as a `QueryDefect` instead of duplicating rows. Narrowing has a cost: isolating one heavy campaign from the full id range takes up to 63 halvings, about 126 calls, and every one of them counts against the call budget.

## Safe absence

A tracked campaign is marked `ended`, or raises a `missing` discrepancy, only from complete reads. Both of these checks must pass in the same entity sync:

1. The Q-G2 keyset scan ended on a short page, with no failed or deferred page.
2. Every Q-G2b chunk for the tracked campaigns the scan no longer returned succeeded.

Then a campaign that Q-G2b returns as `REMOVED` is snapshotted as `ended`, and one that neither query returned raises a `missing` discrepancy. If either check fails, `google.entities` is `incomplete` for that sync, Q-G2b is not run after an incomplete scan, nothing is inferred, and the previous snapshot stands. Ad groups (Q-G4) and keywords (Q-G7, keyed by ad group and criterion id) follow the same rule. The discrepancy check (C0) is described in [the recommendation rules](../recommendation-rules.md).

## Call budget and quotas

`GOOGLE_DAILY_CALL_BUDGET` (default `1000`) caps the product's webhook calls per sliding 24 h. It is the product's own cap, enforced by `GoogleCallBudget` in `sync/budget_guard.py`:

- **Every POST attempt counts**: first pages, next-page calls, partition calls and retries. The client asks the budget before each attempt, and a refused attempt sends nothing.
- **One cap for every process**: the count lives on the `google` row of `source_connection` (`calls_by_minute` in UTC minute buckets, summed into `calls_24h`). The worker, `ads-engine sync` and `ads-engine capture-fixtures` all share it, and `/sources` shows `calls_24h` against the cap.
- **Exhaustion is a deferral, never a retry.** The remaining pages are deferred to a later window, with the reason "call budget exhausted; N partitions deferred". The affected datasets keep their last good data and stay `incomplete` until the deferred pages finish, so items reading them carry the `incomplete` flag. The entity sync infers no removal meanwhile, and the next metrics run widens its range to re-read the shortfall. A dataset whose coverage stops short of the window end, or whose last success is older than `sources.stale_after_hours` (24) in `config/thresholds.yaml`, also makes those items `stale` (see [the architecture overview](../architecture.md)).
- **Simulated calls are unmetered.** When Google is simulated, the client is given an unmetered budget (`UNMETERED`), so a simulated n8n call never counts as a live Google call.

The real quota is not the product's. The Google Ads access level and its daily operation limit belong to the developer token inside n8n's Google Ads credential, set through that token's Google Cloud project. The product cannot see it, and every other n8n workflow using the same credential draws on it.

| Access level | Production operations per day |
|--------------|-------------------------------|
| Explorer | 2,880 |
| Basic | 15,000 |
| Standard | Unlimited |

What counts against that quota:

- A first-page `search` request counts as one operation.
- A follow-up request carrying a valid `next_page_token` does not count. One with an expired or invalid token raises an error and does count.
- A request rejected with a `GoogleAdsFailure` still counts.
- A failure that never reaches the API, such as a network error, does not count.

Exceeding the quota returns `RESOURCE_EXHAUSTED`. The default cap of 1,000 calls sits below Explorer's 2,880 operations because the quota is shared. Lower the cap further when other n8n workflows draw heavily on the same credential.

Google limits granular reporting data to 37 months from June 2026. The default 180-day backfill is well inside that limit.

## Errors and fallback

Connectors raise only `AccessDenied`, `TransientError` or `QueryDefect` (`connectors/errors.py`), and `sync/mode.py` alone decides the consequence. Body signals are read before the HTTP status, because n8n can relay a Google error under any status, including 200. When both apply, access denied outranks transient, and transient outranks defect. Google errors arrive either as the REST envelope `{"error": {code, message, status, details[]}}` or as a bare `google.rpc` Status `{code, message, details[]}` relayed as the whole body; both are read.

| Condition | Class | Behaviour |
|-----------|-------|-----------|
| `N8N_GOOGLE_ADS_WEBHOOK_URL`, `N8N_WEBHOOK_SECRET` or `GOOGLE_ADS_CUSTOMER_ID` missing | Access unavailable | Google runs on simulated data, reason "credentials not configured" |
| HTTP 401 or 403, or a body carrying `PERMISSION_DENIED`, `UNAUTHENTICATED`, `AuthorizationError` or `AuthenticationError`, even with HTTP 200 from n8n | Access denied | Google switches to simulated data, with the platform code and the failing query as the reason |
| HTTP 429 or 5xx, a timeout or network failure, `RESOURCE_EXHAUSTED`, or the call budget exhausted | Transient | 3 attempts in total with exponential backoff (2 s, then 4 s, each plus up to 1 s of jitter). `Retry-After`, or Google's `retryDelay`, is honoured in full up to 120 s; a longer requested wait ends the call at once. Budget exhaustion is never retried; it defers the remaining pages (see [Call budget and quotas](#call-budget-and-quotas)). Afterwards the affected datasets keep their last good data and turn stale. Never mock |
| HTTP 400 or any other 4xx, a GAQL `QueryError`, or a validation error | Defect | The job fails and is logged with the query id. No retry, no mock |

n8n has been observed to answer an invalid GAQL query with HTTP 500 and `{"message": "Error in workflow"}`, without the Google error body. At the HTTP level that defect is indistinguishable from a transient 5xx, so it is retried and the dataset turns stale. When a Google dataset stays stale while other n8n workflows run normally, check the failing query id in the worker's logs and the workflow's executions in n8n.

**Modes.** `DATA_MODE=auto` (the default) applies the table above. `DATA_MODE=simulated` forces simulated data, never probes and never promotes Google to live. A Google dataset is entirely live or entirely simulated. Switching mode builds and backfills a new dataset, which replaces the previous one in one transaction; until then the previous dataset stays active and labelled.

**Recovery.** While Google is in automatic fallback (access denied under `auto` with credentials present), a recovery probe runs Q-G1 every 30 minutes. When it passes, the worker builds and backfills a live dataset and activates it only once the backfill completes; a failure during the build discards the building dataset and leaves the simulated one active. Credentials added before a restart also go live through this path. Datasets, activation and fallback are described in [the architecture overview](../architecture.md).

## Day boundaries and search-term coverage

Google's daily rows use Google's native day: `segments.date` is a date in the account's `customer.time_zone`, read by Q-G1 and stored on the account. Dates are stored as returned and never rebucketed into UTC or `DISPLAY_TIMEZONE`. The incremental range ends on today in that zone. Evidence names the boundary each source used, for example "Google 2026-09-04 to 2026-10-01 (America/New_York)", so Google days are never presented as LinkedIn's UTC days.

Search-term coverage below 100% is expected, because Google withholds low-volume search terms for privacy, so search-term totals fall below keyword totals. The Keywords section shows the coverage as search-term spend ÷ keyword spend, and keyword items carry the `search_term_coverage` flag when it is below `keywords.min_search_term_coverage` (0.80) in `config/thresholds.yaml`.

## Simulated mode and logging

When Google is simulated, by `DATA_MODE=simulated` or because access is unavailable or denied, the same client and parsers run unchanged against a different transport:

- The client posts to the reserved URL `https://n8n.sim.invalid/webhook/google-ads` (`SIMULATED_WEBHOOK_URL`) with a fixed placeholder header value. The `.invalid` top-level domain is reserved, so the URL can never reach a real host, and the simulated transport opens no socket.
- The simulated transport (`simulation/transport.py`) validates each request against the same contract files, so a request missing its secret header or breaking the body contract gets HTTP 400 and fails as a defect, exactly as it would live.
- It answers with seeded data (`MOCK_SEED`, default 42) in the same camelCase envelope, without a page token.
- Calls are unmetered and never touch `calls_24h`.
- Every item built on simulated Google data is labelled simulated in the UI.

Logs never contain the secret or its header value, the webhook URL's path, the customer id, GAQL text, `IN` id lists (only their length) or emails. A URL is logged as its host at most, and the redaction processors in `logging.py` strip the secret header as a second line of defence. Each call logs its query id, page, row count, whether the page was capped, the attempts and the running call count.

## Not reachable through this path

| Capability | Why it is unreachable | What the product uses instead |
|------------|-----------------------|-------------------------------|
| `KeywordPlanIdeaService` | A service RPC, not GAQL, so the webhook cannot relay it; it is also blocked on Explorer access | Adjacent terms for expansion candidates come from the account's own search-term report by token clustering, labelled "from your search-term report" |
| `searchStream` | Excluded by the user | Paginated `googleAds:search` |
| Performance Max search terms (`campaign_search_term_view`) | Not among the resources the user named | Out of scope |
| Write operations (pausing, budget edits, negative keywords) | The product is recommend-only | None. The user applies changes in Google Ads and records them under Decisions |

## Contract files and references

Each Google surface has a contract file in `backend/tests/contracts/google/`. It records the official reference page, the API version and, where recorded, the page's last-modified date, together with the request shape and, for row contracts, `request.gaql.select`. The simulator and the contract tests both read these files, so neither encodes its own assumptions.

| File | Query | `reference_url` | `api_version` | `last_modified` |
|------|-------|-----------------|---------------|-----------------|
| `search.json` | Every call (envelope; `limits`: `page_size` 10000, `timeout_s` 120, `keyset_limit` 9999, `in_list_max` 1000) | <https://developers.google.com/google-ads/api/reference/rpc/v25/GoogleAdsService/Search?transport=rest> | `v25` | 2026-07-22 |
| `row_customer.json` | Q-G1 | <https://developers.google.com/google-ads/api/fields/v25/customer> | `v25` | Not recorded (`null`) |
| `row_campaign.json` | Q-G2 and Q-G2b | <https://developers.google.com/google-ads/api/fields/v25/campaign> | `v25` | 2026-09-30 |
| `row_campaign_daily.json` | Q-G3 | <https://developers.google.com/google-ads/api/fields/v25/campaign> | `v25` | Not recorded (`null`) |
| `row_ad_group.json` | Q-G4 | <https://developers.google.com/google-ads/api/fields/v25/ad_group> | `v25` | Not recorded (`null`) |
| `row_keyword_view.json` | Q-G5 | <https://developers.google.com/google-ads/api/fields/v25/keyword_view> | `v25` | 2026-09-30 |
| `row_search_term_view.json` | Q-G6 | <https://developers.google.com/google-ads/api/fields/v25/search_term_view> | `v25` | 2026-09-30 |
| `row_ad_group_criterion.json` | Q-G7 | <https://developers.google.com/google-ads/api/fields/v25/ad_group_criterion> | `v25` | Not recorded (`null`) |
| `row_conversion_action.json` | Q-G8 | <https://developers.google.com/google-ads/api/fields/v25/conversion_action> | `v25` | 2026-09-30 |

Further official references:

- REST search guide, fixed pages of 10,000 rows and `nextPageToken`: <https://developers.google.com/google-ads/api/rest/common/search>
- Paging through results: <https://developers.google.com/google-ads/api/docs/reporting/paging>
- Access levels and daily operation limits: <https://developers.google.com/google-ads/api/docs/api-policy/access-levels>
- Quotas, including which requests count as operations: <https://developers.google.com/google-ads/api/docs/best-practices/quotas>
- n8n Webhook node authentication (Header Auth): <https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/>

**Live captures.** Once the Google credentials work, `ads-engine capture-fixtures --source google` records one redacted live request and response per query into `backend/tests/contracts/google/captured/`. Run it from `backend/` on the host, or from `/app` inside the backend image:

```bash
cd backend
uv run --frozen ads-engine capture-fixtures --source google
```

- It sends one call per template (Q-G1 to Q-G8 and Q-G2b), reads the first page only and counts every call against `GOOGLE_DAILY_CALL_BUDGET`. When the database is unreachable it caps itself at one call per template instead, and those calls are not recorded in `calls_24h`.
- It is refused, with nothing sent or written, under `DATA_MODE=simulated` or without the Google credentials.
- The first call becomes `captured/search.json`, and the first row of each query becomes its `captured/row_*.json`. Each file is shaped `{surface, captured_at, request, response}`.
- The secret header value, emails and names are replaced by `"<redacted>"`; the webhook URL is replaced by a reserved placeholder URL, and the customer id by zeros of the same length.
- The contract tests then run against the captured payloads as well as the contracts.
