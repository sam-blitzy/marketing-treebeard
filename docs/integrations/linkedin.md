# LinkedIn Marketing API

The LinkedIn connector reads the company's LinkedIn ad accounts directly through the LinkedIn
Marketing API. It covers ad accounts, campaign groups, campaigns and creatives in every status,
the posts creatives reference, daily creative performance, preset-window demographic segments,
lead forms, and lead form responses traced to the creative that produced them. It only reads.
The product never creates, updates or deletes anything on LinkedIn, and it has no OAuth consent
screen.

This page is for two readers: the operator who grants LinkedIn access, and the developer who
changes the connector. It describes what the code does. The code lives in
`backend/src/ads_engine/connectors/linkedin/`:

| Module | Holds |
|--------|-------|
| `client.py` | `LinkedInClient` and every request it sends; the Rest.li helpers `encode_urn`, `restli_list`, `restli_date_range` and `restli_query`; the result types `Listing`, `LeadListing`, `LeadBatch` and `PostBatch` |
| `auth.py` | `LinkedInTokenProvider` (refresh-token exchange or static token), `StaticTokenProvider`, `TOKEN_URL`, `SIMULATED_TOKEN` |
| `parser.py` | Typed rows, the status lists and maps, form identity (`parse_versioned_form_urn`, `canonical_form_urn`) and email decoding (`parse_lead`, `decode_email`) |

Related pages: [architecture](../architecture.md) (components, schedule, fallback),
[recommendation rules](../recommendation-rules.md) (C0, C3a and the attribution floors),
[runbook](../runbook.md) (setup, token bootstrap, recovery), [HubSpot](hubspot.md) (lead
matching) and the [project README](../../README.md).

## Version, base URL and headers

The connector is pinned to Marketing API version `202609` and Rest.li protocol `2.0.0`
(`RESTLI_PROTOCOL_VERSION`). The version pin lives in `settings.py` as `LINKEDIN_API_VERSION`,
and the client exposes it as `LINKEDIN_VERSION`. LinkedIn supports each monthly version for at
least a year. Every request path is relative to `https://api.linkedin.com/rest/`, and every request
is a `GET` carrying these headers:

```http
GET /rest/adAccounts/<account id> HTTP/1.1
Host: api.linkedin.com
Authorization: Bearer <access token>
Linkedin-Version: 202609
X-Restli-Protocol-Version: 2.0.0
Accept: application/json
```

The posts batch get also sends `X-RestLi-Method: BATCH_GET`. The OAuth token endpoint is the one
exception to these headers (see [Access tokens](#access-tokens)).

Every call goes through `send_json` in `backend/src/ads_engine/connectors/http.py`, which gives
LinkedIn a 60 s deadline per call (10 s to connect) and owns the retry policy described under
[Errors and fallback](#errors-and-fallback).

## Configuration

The variables live in the root `.env.example`, where every secret is empty. Set real values only
in the server-side `.env`, which is never committed and never reaches the browser bundle. A blank
value counts as unset.

| Variable | `.env.example` value | Kept as | Meaning |
|----------|----------------------|---------|---------|
| `LINKEDIN_AD_ACCOUNT_IDS` | empty | list of ids | Comma-separated numeric sponsored ad account ids. Settings also accept `urn:li:sponsoredAccount:<digits>` entries, drop blank entries and de-duplicate in first-seen order. Any other entry fails settings validation |
| `LINKEDIN_CLIENT_ID` | empty | plain string | OAuth client id of the refresh-token trio |
| `LINKEDIN_CLIENT_SECRET` | empty | `SecretStr` | OAuth client secret of the trio |
| `LINKEDIN_REFRESH_TOKEN` | empty | `SecretStr` | Refresh token of the trio, exchanged for an access token held in memory only |
| `LINKEDIN_ACCESS_TOKEN` | empty | `SecretStr` | Static access token, the alternative to the trio. The trio wins when both are set |

LinkedIn can run live only with `LINKEDIN_AD_ACCOUNT_IDS` plus either the complete trio or
`LINKEDIN_ACCESS_TOKEN`. Without them, LinkedIn runs on simulated data with the reason
"credentials not configured", and nothing is sent to LinkedIn.

Other settings that shape LinkedIn behaviour:

| Variable | Default | Effect on LinkedIn |
|----------|---------|--------------------|
| `DATA_MODE` | `auto` | `auto` falls back to simulated data per source; `simulated` forces it and never probes LinkedIn |
| `MOCK_SEED` | `42` | Seed of the simulated world |
| `BACKFILL_DAYS` | `180` | Initial backfill of each new dataset, capped at LinkedIn's 180-day daily retention |
| `DISPLAY_TIMEZONE` | `UTC` | Wall-clock zone of the sync schedule and of displayed instants. It never moves LinkedIn's UTC day boundary |

## Access tokens

`LinkedInTokenProvider` picks one of two modes at startup:

- **Refresh mode**: `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` and `LINKEDIN_REFRESH_TOKEN`
  are all set. The provider exchanges the refresh token for an access token with one
  form-encoded `POST` to `TOKEN_URL`.
- **Static mode**: otherwise, when `LINKEDIN_ACCESS_TOKEN` is set. The provider returns that
  token with no network call and cannot refresh it.

The exchange (contract surface `access_token`) is unversioned. It sends no `Authorization`,
`Linkedin-Version` or `X-Restli-Protocol-Version` header, and its body is exactly four form
fields:

```http
POST /oauth/v2/accessToken HTTP/1.1
Host: www.linkedin.com
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=<refresh token>&client_id=<client id>&client_secret=<client secret>
```

- **Caching**: the access token is cached in memory only, until 300 s
  (`EXPIRY_MARGIN_SECONDS`) before its `expires_in` runs out. Expiry is measured from the instant
  before the request was sent, so the cache can only expire early. A token with `expires_in` of
  300 s or less is used for that call and not cached. Concurrent callers share one exchange.
- **Rotation**: when LinkedIn returns a new refresh token, it replaces the configured one in
  memory only. It is never persisted or logged, so a restarted process starts again from
  `LINKEDIN_REFRESH_TOKEN`. The provider keeps the last 8 tokens it stopped using (refresh
  tokens rotated away, access tokens dropped or replaced) in memory as well, only so that an
  error echoing one is stripped of it before it is logged.
- **Rejected credentials**: OAuth `invalid_grant`, `invalid_client` or `unauthorized_client`,
  any 401, and a 400 `invalid_request` whose description says the grant or token is expired or
  revoked are access denied. LinkedIn then switches to simulated data. Any other 400, or a 200
  without a usable `access_token` and positive `expires_in`, is a defect.
- **A 401 from the REST API**: in refresh mode the client drops the cached token, exchanges
  again and retries the call once. A second 401 is access denied. With a static token the first
  401 is access denied.

Tokens are bootstrapped once with the LinkedIn Developer Portal token generator, as the
[runbook](../runbook.md) describes. The product has no consent UI and runs no
authorization-code flow.

## Scopes and access tiers

The token needs these read-only scopes:

| Scope | Unlocks | Used by |
|-------|---------|---------|
| `r_ads` | Ad accounts, campaign groups, campaigns and creatives | Entity sync, recovery probe |
| `r_ads_reporting` | `adAnalytics` | Daily metrics, demographic segments, recovery probe |
| `r_marketing_leadgen_automation` | Lead forms and lead form responses. It requires the Lead Sync API product; `r_ads` cannot read leads | Entity sync (forms), lead sync, the email re-fetch for HubSpot matching, recovery probe |

The Advertising API starts at the Development tier and is upgraded to Standard.

**Flagged risk: reading posts.** Creative headline and intro text come from the Posts API
(contract `posts.json`). LinkedIn documents post reads under `r_organization_social`, which
needs a page role of `ADMINISTRATOR`, `DIRECT_SPONSORED_CONTENT_POSTER` or `CONTENT_ADMIN`, or
under the restricted `r_member_social`. Neither is among the three scopes above. `client.py`
handles a posts failure as follows:

- **One unreadable post**: LinkedIn answers HTTP 200 with a per-key 403 in `statuses` and
  `errors`. That post is a failed read in `PostBatch.failed`, and its creative keeps a null
  headline and intro text. The rest of the batch is read.
- **The whole call returns HTTP 403**: this is `AccessDenied` and is not swallowed. Like any
  LinkedIn denial, it switches all of LinkedIn to simulated data. The recovery probe does not
  read posts, so the probe can pass while the live build's entity sync is denied. The build then
  fails, and LinkedIn stays on its simulated dataset rather than flipping back and forth.

The product requests no extra scope. The operator either grants post read access to the token's
member or accepts simulated LinkedIn data, as the "Creative text" item under
[LinkedIn token bootstrap](../runbook.md#linkedin-token-bootstrap) says.

## Request encoding

`{acct}` below stands for the URL-encoded sponsored account URN,
`urn%3Ali%3AsponsoredAccount%3A<id>` (`encode_urn("urn:li:sponsoredAccount:<id>")`). The
connector sends one set of requests per id in `LINKEDIN_AD_ACCOUNT_IDS`; `{id}` in a path is the
bare numeric id.

Rest.li 2.0 values keep their structure raw and encode only what sits inside it:

- **Structure stays literal**: `(`, `)`, `:` and `,`, and the `List(...)` wrapper.
- **Encoded exactly once**: URNs, ids and page tokens inside the structure are percent-encoded
  once. `encode_urn` refuses input that already holds a `%`, so nothing is double-encoded (no
  `%253A`).
- **Checked before sending**: `restli_query` rejects any key or value holding a raw `"`, space,
  `&`, `#`, `+` or `=`, or a malformed `%` escape. httpx keeps the built query byte for byte.
- **Dates and instants**: dates are `(year:Y,month:M,day:D)` with no zero padding, and both ends
  of a `dateRange` are inclusive. Lead time ranges are epoch milliseconds.
- **Projections**: every `fields` value is sent raw. LinkedIn 202609 rejects a fully
  percent-encoded projection with HTTP 400 "Projected field ... not present in schema".

```text
accounts=List(urn%3Ali%3AsponsoredAccount%3A<id>)
campaigns=List(urn%3Ali%3AsponsoredCampaign%3A<id>,urn%3Ali%3AsponsoredCampaign%3A<id>)
dateRange=(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:28))
owner=(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A<id>)
submittedAtTimeRange=(start:<epoch ms>,end:<epoch ms>)
```

## Endpoints

Each surface has a contract file in `backend/tests/contracts/linkedin/` with the same name. The
requests, as `client.py` builds them:

```text
GET adAccounts/{id}
GET adAccounts/{id}/adCampaignGroups?q=search&search=(status:(values:List(ACTIVE,PAUSED,ARCHIVED,CANCELED,DRAFT,PENDING_DELETION,REMOVED)))&pageSize=100[&pageToken=<token>]
GET adAccounts/{id}/adCampaigns?q=search&search=(status:(values:List(ACTIVE,PAUSED,ARCHIVED,COMPLETED,CANCELED,DRAFT,PENDING_DELETION,REMOVED)))&pageSize=100[&pageToken=<token>]
GET adAccounts/{id}/creatives?q=criteria&campaigns=List({campaign URNs})&intendedStatuses=List(ACTIVE,PAUSED,DRAFT,ARCHIVED,CANCELED,PENDING_DELETION,REMOVED)&pageSize=100[&pageToken=<token>]
GET posts?ids=List({post URNs})                                    (header X-RestLi-Method: BATCH_GET)
GET adAnalytics?q=analytics&pivot=CREATIVE&timeGranularity=DAILY&dateRange=(start:(year:Y,month:M,day:D),end:(year:Y,month:M,day:D))&accounts=List({acct})&fields=pivotValues,dateRange,costInLocalCurrency,impressions,clicks,landingPageClicks,oneClickLeadFormOpens,oneClickLeads,externalWebsiteConversions
GET adAnalytics?q=statistics&pivots=List(CAMPAIGN,{pivot})&timeGranularity=ALL&dateRange=({preset window})&accounts=List({acct})&fields=pivotValues,costInLocalCurrency,impressions,clicks,oneClickLeads,externalWebsiteConversions
GET leadForms?q=owner&owner=(sponsoredAccount:{acct})&count=100&start={k}
GET leadFormResponses?q=owner&owner=(sponsoredAccount:{acct})&leadType=(leadType:SPONSORED)&limitedToTestLeads=false&submittedAtTimeRange=(start:{epoch ms},end:{epoch ms})&count=100&start={k}&fields={LEAD_FIELDS}
GET leadFormResponses?ids=List({id1},{id2},...)&owner=(sponsoredAccount:{acct})&leadType=(leadType:SPONSORED)&fields={LEAD_FIELDS}
```

| Surface | Client method | What it reads | Paging and batching |
|---------|---------------|---------------|---------------------|
| `ad_account` | `get_ad_account` | The account name and currency; also the first recovery-probe call. The currency is remembered for later budget checks | None |
| `ad_campaign_groups` | `list_campaign_groups` | Status, `totalBudget`, `dailyBudget` and run schedule. The seven statuses are `CAMPAIGN_GROUP_SEARCH_STATUSES`. A group's bare `dailyBudget` needs the account currency, so the listing reads `adAccounts/{id}` first when the currency is not yet known | `pageSize=100`; `metadata.nextPageToken` is sent back as `pageToken` until it is absent, null or empty. A repeated token is a defect |
| `ad_campaigns` | `list_campaigns` | Status, `dailyBudget`, `totalBudget`, `campaignGroup` and `objectiveType`. The eight statuses are `CAMPAIGN_SEARCH_STATUSES` | As above |
| `creatives` | `list_creatives` | Campaign, `intendedStatus`, `content.reference` (the post) and the lead-gen call-to-action `destination` and `label` | At most 100 campaign URNs per request (`CREATIVE_CAMPAIGN_CHUNK`), each chunk paged by `pageToken`. An empty campaign list sends no request, because LinkedIn reads `campaigns=List()` as "no filter" |
| `posts` | `get_posts` | Headline (`content.media.title` or `content.article.title`) and intro text (`commentary`). Only `urn:li:share:` and `urn:li:ugcPost:` URNs with a numeric id are requested | At most 50 URNs per call (`POST_BATCH_SIZE`); batch envelope `{results, statuses, errors}` |
| `ad_analytics` | `analytics_daily` | Daily per-creative spend, impressions, clicks, landing-page clicks, lead form opens, leads and website conversions, into `fact_creative_daily` | Date chunks sized to the response cap; `next` links followed (see [Limits and retention](#limits-and-retention)) |
| `ad_analytics_statistics` | `analytics_segments` | Demographic segments per campaign for one exact preset window, into `fact_segment_window` (see [Demographic segments](#demographic-segments)) | `next` links followed |
| `ad_analytics_campaign_delivery` | `analytics_segments` (live fallback) | The account's campaigns with delivery in the window, after LinkedIn rejects the Statistics Finder with HTTP 400 | `next` links followed; cached per account and window |
| `ad_analytics_member_segments` | `analytics_segments` (live fallback) | One pivot's demographic values for one delivering campaign; the campaign is taken from the request | One request per delivering campaign; `next` links followed |
| `lead_forms` | `list_lead_forms` | Each form's canonical URN, current `versionId` and that version's `content.questions` | `count`/`start` (see [Lead Sync](#lead-sync)) |
| `lead_form_responses` | `list_lead_responses` | Sponsored lead responses submitted in a time range | `count`/`start` |
| `lead_form_responses_batch` | `get_lead_responses` | The transient email re-fetch by response id, for HubSpot matching | At most 100 ids per call (`LEAD_BATCH_SIZE`) |

Statuses are kept raw and normalised for analysis through `CAMPAIGN_STATUS_MAP`: `ACTIVE` becomes
`active`; `PAUSED` and `DRAFT` become `paused`; `ARCHIVED`, `COMPLETED`, `CANCELED`,
`PENDING_DELETION` and `REMOVED` become `ended`. Groups and creatives use maps of the same form.
The group map also tolerates `CANCELLED` and `COMPLETED`, so a real response is never rejected.
Budgets are decimal strings in the ad account's currency. A budget in another currency is a
defect, never a conversion.

### Corrections the code applies to the plan

- **`COMPLETED` campaigns**: the plan reuses the group status list for campaigns. LinkedIn
  documents an eighth campaign status, `COMPLETED` (a budgetary or chronological limit
  reached), so `CAMPAIGN_SEARCH_STATUSES` adds it. Without it, every finished campaign would
  raise a false `missing` discrepancy. The group list stays at seven, because `COMPLETED` is not
  a documented group search value and an undocumented enum value risks HTTP 400.
- **`intendedStatuses` on creatives**: LinkedIn hides `CANCELED` creatives from a campaign query
  unless they are requested. The client sends the full `CREATIVE_INTENDED_STATUSES` list, so a
  hidden creative cannot drop its spend and leads from white-paper attribution.
- **`X-RestLi-Method: BATCH_GET` on posts**: the Posts API page requires it for a batch get.
- **Lead Sync**: the projection is sent raw, batch re-fetch ids are unquoted, and paging ignores
  the page-local `paging.total`. Each is explained under [Lead Sync](#lead-sync).

**Safe absence.** A tracked campaign absent from a complete listing raises a `missing`
discrepancy (rule C0 in [recommendation rules](../recommendation-rules.md)). A listing is
complete only when the campaign-group and campaign listings of every configured account read all
their pages. A listing cut short by a failed page returns its rows with `complete=False`, marks
`linkedin.entities` incomplete with the reason, and infers no removal or disappearance.

### Demographic segments

Segments feed launch rule C3a (an underused but proven segment). `SegmentSyncJob` requests each
pivot in `SEGMENT_PIVOTS`, for each preset in `window.presets` (`[14, 28, 56, 90]` in
`config/thresholds.yaml`), for each ad account:

| Pivot | Stored facet | Value URN |
|-------|--------------|-----------|
| `MEMBER_JOB_FUNCTION` | `job_function` | `urn:li:function:<id>` |
| `MEMBER_SENIORITY` | `seniority` | `urn:li:seniority:<id>` |
| `MEMBER_INDUSTRY` | `industry` | `urn:li:industry:<id>` |

Each preset of `n` days covers `[end - n + 1, end]`, where `end` is the default window end: the
earlier of yesterday in the Google account's time zone and yesterday UTC. Every preset window
therefore ends on the default window end; only its start differs. The preset of
`window.default_days` (28 by default, always in `window.presets`) equals the default run's window
exactly, first start included. A run over any other preset length that ends on the default end
matches that preset's fetched window.

**Flagged risk, and the form the client uses.** The contracted request (the plan and
`ad_analytics_statistics.json`) is the Statistics Finder:
`q=statistics&pivots=List(CAMPAIGN,{pivot})&timeGranularity=ALL&accounts=List({acct})`. The
2026 Reporting schema page lists no `MEMBER_*` value in the Statistics Finder `pivots` enum.
Professional-demographic pivots are documented only for the Analytics Finder `pivot`. A live
202609 request in the contracted form returned HTTP 400 `FIELD_INVALID` at `pivots_1/value`
("is not an enum symbol") on 2026-10-07.

`client.py` therefore works as follows:

- **First request**: it sends the contracted Statistics Finder request.
- **On HTTP 400**: it switches, for the rest of that client's life, to the documented Analytics
  Finder fallback. One discovery call finds the account's campaigns with delivery in the window.
  Then one request per campaign, pivot and preset reads the segment, and the campaign is taken
  from the request. Each form has its own contract, `ad_analytics_campaign_delivery.json` and
  `ad_analytics_member_segments.json`, and `payloads.identify_surface` routes it there by `q`,
  `pivot` and `timeGranularity`:

  ```text
  GET adAnalytics?q=analytics&pivot=CAMPAIGN&timeGranularity=ALL&dateRange=({window})&accounts=List({acct})&fields=pivotValues,impressions
  GET adAnalytics?q=analytics&pivot={pivot}&timeGranularity=ALL&dateRange=({window})&campaigns=List({one campaign URN})&fields=pivotValues,costInLocalCurrency,impressions,clicks,oneClickLeads,externalWebsiteConversions
  ```

  Campaigns reported with zero impressions are skipped. The discovery result is cached per
  account and window, so the three pivots of one preset share one discovery call. A defect in
  the fallback itself fails the job, chained to the original 400.
- **In simulated mode**: the simulator answers the contracted form, so simulated runs never take
  the fallback unless a world fault rejects `ad_analytics_statistics` with HTTP 400. The
  simulator validates and renders both fallback forms from the same world as the Statistics
  Finder: campaign delivery is each campaign's impressions over the window, and member segments
  are the demographic values of the one requested campaign.

**Segment semantics.**

- **Approximated values**: LinkedIn approximates demographic metrics and applies privacy
  thresholds. It returns at most the top 100 values (per creative per day, as the Reporting page
  states), drops values with fewer than 3 events, returns no values for ads that reached too few
  members, and delays these metrics by 12 to 24 hours.
- **Exact windows only**: daily rows or narrower windows do not sum to a window's value, so each
  preset window is fetched as one aggregate and stored as returned in `fact_segment_window`,
  keyed by its exact start and end.
- **Missing metrics**: a metric the pivot does not return is stored as NULL (unavailable), never
  0.
- **Response cap**: a response at the 15,000-element cap without a `next` link makes the listing
  incomplete with the reason "result limit reached". The window must stay exact, so it is never
  split by date.
- **Use by C3a**: C3a runs only on a run whose window exactly equals a fetched segment window.
  Otherwise, while at least one LinkedIn campaign is active, it is withheld with
  `segment_window_unavailable` and never reuses another window's segments (see
  [recommendation rules](../recommendation-rules.md)).

## When each call runs

The worker's scheduler uses wall-clock times in `DISPLAY_TIMEZONE` and wakes every 30 s. The
full schedule and the analysis refresh after each job are in [architecture](../architecture.md).

| Job | Cadence | LinkedIn calls | Freshness dataset |
|-----|---------|----------------|-------------------|
| Entity sync | Every 3 h (00:00, 03:00, ...) | `adAccounts/{id}`, campaign groups, campaigns, creatives, posts, lead forms | `linkedin.entities` |
| Metrics, incremental | Every 6 h (00:00, 06:00, 12:00, 18:00), trailing 3 UTC days | Daily `adAnalytics` | `linkedin.analytics_daily` |
| Metrics, restatement | Nightly at 03:00, trailing 14 UTC days, upserted over earlier values | Daily `adAnalytics` | `linkedin.analytics_daily` |
| Demographic segments | Nightly at 04:00; also on any wake where the default window end has passed the latest fetched segment end or `window.presets` changed (a failed fetch is retried at most every 30 minutes); also inside every new LinkedIn dataset's backfill | Segment `adAnalytics` per account, pivot and preset | `linkedin.segments` |
| Lead responses | Every 3 h, from the last success minus 1 h to now | `leadFormResponses` by owner | `linkedin.leads` |
| HubSpot matching | Every 3 h, inside the HubSpot job (see [HubSpot](hubspot.md)) | `leadFormResponses` batch re-fetch by id | HubSpot datasets |
| Recovery probe | Every 30 min while LinkedIn is in automatic fallback | The probe set (see [Recovery](#recovery)) | None |
| Initial backfill | First run and every new dataset | Everything above | All four |

The backfill covers `BACKFILL_DAYS` (default 180), capped at 180 days, LinkedIn's 6-month daily
retention. Every daily range starts no earlier than 179 days before today in UTC.

## Limits and retention

- **Fields**: at most 20 `fields` per `adAnalytics` request. The daily projection
  (`DAILY_FIELDS`) names 9 and the segment projection (`SEGMENT_FIELDS`) names 6. `pivotValues`
  is a response field only, requested through `fields` and never sent as a filter. `dateRange` is
  the required parameter that selects the days, and, through `fields`, the response field that
  dates each element.
- **15,000 elements per response** (`MAX_ANALYTICS_ELEMENTS`): daily analytics are chunked so
  creatives × days stays below the cap.
  - **Chunk size**: `max(1, 14,999 // n)` days, where `n` is the number of creatives the
    dataset holds for the account, or else the count from the client's latest creative listing
    of that account. With no known count, the whole range is one chunk.
  - **`next` links**: a `next` paging link is followed at once, even from an empty page, for at
    most 1,000 links per chain. A link that repeats an earlier request is a defect.
  - **Split at the cap**: a response with 15,000 or more elements and no `next` link may be
    truncated. Its rows are discarded and its range is halved into `[start, mid]` and
    `[mid + 1, end]`.
  - **One day at the cap**: the rows are kept, and the listing and `linkedin.analytics_daily` are
    marked incomplete with the reason "result limit reached at finest partition (one day)".
  - Completeness never depends on the creative count, because any chunk at the cap is split.
- **Retention**: daily data is kept for 6 months. That caps the backfill, and any custom window
  (`window.max_days: 180`), at 180 days. A `timeGranularity=ALL` range reaching past the daily
  retention is rounded to month boundaries; the preset windows end yesterday and are at most 90
  days by default, so they stay inside it. Demographic data is kept for about 2 years and other
  performance data for 10 years.
- **Entity pages**: `pageSize=100` everywhere (documented maximum 1,000 for campaign groups and
  campaigns, 100 for creatives). An account holds at most 15,000 creatives, a campaign at most
  100.
- **Throttling**: `adAnalytics` is throttled at 45 million metric values per 5 minutes. Daily
  per-application and per-member limits are unpublished: they are visible in the Developer
  Portal and reset at midnight UTC. Exceeding a limit returns 429, which is transient.

## Lead Sync

Lead Sync links each lead to the creative that produced it. Its calls need
`r_marketing_leadgen_automation`.

### Projection

Both lead-response requests send this `fields` projection, `LEAD_FIELDS`:

```text
ownerInfo,associatedEntityInfo,leadMetadataInfo,owner,leadType,versionedLeadGenFormUrn,id,submittedAt,testLead,formResponse,form:(hiddenFields,creationLocale,name,id,content)
```

- **Origin**: it is the "Response Decoration Migration" projection of LinkedIn's Lead Sync API
  migration guide, verbatim. It is the `request.fields` of `lead_form_responses.json` and
  `lead_form_responses_batch.json`; `connectors/linkedin/projections.py` generates `LEAD_FIELDS`
  from them into `_projections_generated.py`, and a unit test fails while that module is stale.
- **Encoding**: RFC 3986 allows `,`, `:`, `(` and `)` in a query, so the raw value is already its
  single URL encoding: percent-decoding it once returns `LEAD_FIELDS` exactly. The client sends
  it raw. The plan text asked for it percent-encoded as a whole, but live 202609 rejects that
  form with HTTP 400 "Projected field ... not present in schema", so the contract wins.
- **Decoration**: the projection decorates each response with a `form` object that LinkedIn
  builds from that response's own `versionedLeadGenFormUrn`.
- **Projected paths**: under the projection LinkedIn returns only the projected keys. The
  creative comes from `associatedEntityInfo.associatedCreative.id` and the campaign from
  `leadMetadataInfo.sponsoredLeadMetadataInfo.campaign.id`. The parser falls back to
  `associatedEntity.associatedCreative` and `leadMetadata.sponsoredLeadMetadata.campaign`, the
  paths of the unprojected documented sample. When both paths are present they must agree.

### Paging

Lead forms and lead responses page by `count` and `start`: `count=100`, then `start` advances by
`count`.

- **Envelope check**: each page's `paging.start` and `paging.count` must echo the request, or the
  page is a defect.
- **Stop rule**: `paging.total` counts only the elements of the current page. The documented
  sample shows `count` 10 with `total` 1; live 202609 returned `total` 100 with a `next` link at
  `count=100`. A `start + count ≥ total` rule would therefore stop after page one. The client
  stops on a page with fewer than `count` elements and no `next` link, or on an empty page.
- **Template, not href**: the `next` href reorders the projection and the time range, so it is
  never followed; every page is rebuilt from the request template.
- **Forms**: lead forms page the same way. Under an overall `total` this costs at most one extra,
  empty page instead of a missed one.

### Test leads and the incremental window

- **Test leads**: `limitedToTestLeads=false` returns test and real leads together. A response with
  `testLead=true` is dropped at ingest, counted in `test_leads_skipped` and never stored.
- **Window**: `LeadSyncJob` requests responses submitted from the last successful
  `linkedin.leads` sync minus 1 h (`LEAD_OVERLAP`) to now. The first run and every backfill read
  `min(BACKFILL_DAYS, 180)` days.
- **De-duplication**: responses are de-duplicated on their response id within a listing, and the
  `lead` table is unique per dataset and response id, so the overlap is harmless.

### Form identity

A response's `versionedLeadGenFormUrn` has the form
`urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{id},{version})`.
`parse_versioned_form_urn` splits it into the canonical `urn:li:leadGenForm:{id}` and an integer
version, and the lead stores both.

- **Version source**: the version comes only from this URN. The `form` decoration carries
  `form.id` but no version field, and `form.id` is never read for identity.
- **Canonical joins**: the content-asset registry (`config/content_assets.yaml`) and creative
  call-to-action destinations join on the canonical URN, so every version of a form maps to the
  same white paper.
- **Normalisation**: `canonical_form_urn` turns a call-to-action destination of the form
  `urn:li:adForm:{id}` into `urn:li:leadGenForm:{id}`. It also accepts a canonical URN unchanged
  and a versioned URN with its version dropped.

### Email decoding

Editing a form creates a new version, so a question id can change meaning between versions.
Emails are therefore decoded per version, with the maps stored in `lead_form_version` (canonical
URN, version, `email_question_ids text[]`).

- **Map sources**: maps come from each response's decoration, keyed by the version parsed from
  that response's own URN and kept only when it passes the consistency check below, and from the
  `leadForms` sync, for each form's current `versionId`.
- **Email questions**: the email questions are those whose `predefinedField` is `EMAIL`, then
  those with `WORK_EMAIL`, each in ascending id order. `WORK_EMAIL` goes beyond the plan, which
  names only `EMAIL`: it is a distinct documented value, and a form that asks only for a work
  email would otherwise end every lead as `no_email`.
- **Email answer**: the email is the `answerDetails.textQuestionAnswer.answer` of the first email
  question that holds a usable address. That address is trimmed, lowercased and has exactly one
  `@`.

**Consistency check.** Every `questionId` in `formResponse.answers` must appear among the
questions of that response's own decoration. When a stored map is used and it knows its
questions, it is checked the same way. A lead is never decoded with another version's map. A
lead whose email cannot be decoded is staged in `lead_match` as `no_email`, which is terminal,
with one of these reasons:

| Reason | When |
|--------|------|
| `form map mismatch` | An answered question id is missing from the questions of the response's own form version |
| `unknown form version` | The response has no decoration and no stored map exists for its (canonical URN, version) |
| `form has no email question` | The version asks neither an `EMAIL` nor a `WORK_EMAIL` question |
| `no email answer` | No email question holds a usable address |

All four count toward the unresolved-contact share and its attribution floor (see
[recommendation rules](../recommendation-rules.md)).

### Personal data and the transient re-fetch

- **What is stored**: the normalised email is hashed with SHA-256 into `lead.email_sha256`, and
  the plaintext is discarded inside the parser. Free-text answers, consent responses and hidden
  fields are never read into a row or stored.
- **Re-fetch for matching**: each HubSpot matching attempt (see [HubSpot](hubspot.md)) re-fetches
  the lead's response with the batch get by id. It decodes the email with the version's map and
  holds the plaintext in memory for exactly one HubSpot `contacts_by_email` call.
- **Batch request**: a call carries at most 100 plain response ids (LinkedIn documents 1,000).
  Ids are sent unquoted, each percent-encoded once. The plan and the reference page's sample
  quote them (`List("{id}")`), but live 202609 answers quoted ids with HTTP 200 and empty
  `results` and `errors`, which would end every lead as unretrievable.
- **Batch response**: `{results: {"<id>": element}, statuses, errors}`. Keys are percent-decoded
  and stripped of surrounding quotes, and `statuses` and `errors` are read both at the top level
  and nested inside `results`.
- **Identity check**: an element counts as found only when its own `id` is the requested id, its
  `owner.sponsoredAccount` the requested account, its `leadType` `SPONSORED` and its `testLead`
  `false`. An element that fails the check is a failed re-fetch, counted as `identity_rejected`.
- **Failed re-fetch**: an id listed in `errors`, given a non-2xx status, absent from `results` or
  failing the identity check ends as `lead_match.status = error`, with this reason:
  "email no longer retrievable". That is terminal and counts as unresolved.
- **Interrupted batch**: when a transient error ends a batch early, the ids not yet read become
  `error` with that failure's reason and are retried.
- **Malformed response**: a re-fetched response that breaks its contract ends as a terminal
  `error`, with this reason: "re-fetched lead response failed contract validation".

### Creative link

`associatedEntity`, the creative, may be null or absent. Such a lead is credited to the
"unknown creative" row of its white paper, and the creative-share floor counts it.

### Example of a decoded response element

This is one projected element of `leadFormResponses`, with ids from the reserved simulated range
and answer values redacted:

```json
{
  "id": "aaaabbbb-0000-cccc-1111-dddd2222eeee-5",
  "submittedAt": 1790000000000,
  "testLead": false,
  "leadType": "SPONSORED",
  "owner": {"sponsoredAccount": "urn:li:sponsoredAccount:990000001"},
  "versionedLeadGenFormUrn": "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990000101,2)",
  "associatedEntityInfo": {
    "associatedCreative": {"id": "urn:li:sponsoredCreative:990001001"}
  },
  "leadMetadataInfo": {
    "sponsoredLeadMetadataInfo": {"campaign": {"id": "urn:li:sponsoredCampaign:990010001"}}
  },
  "formResponse": {
    "answers": [
      {"questionId": 1, "answerDetails": {"textQuestionAnswer": {"answer": "<redacted>"}}},
      {"questionId": 2, "answerDetails": {"textQuestionAnswer": {"answer": "<redacted>"}}}
    ]
  },
  "form": {
    "id": 990000101,
    "name": "<form name>",
    "content": {
      "questions": [
        {"questionId": 1, "name": "firstName", "predefinedField": "FIRST_NAME"},
        {"questionId": 2, "name": "workEmail", "predefinedField": "WORK_EMAIL"}
      ]
    }
  }
}
```

The parser reads canonical form `urn:li:leadGenForm:990000101`, version 2, creative
`urn:li:sponsoredCreative:990001001` and campaign `urn:li:sponsoredCampaign:990010001`. Both
answered question ids appear among the decorated questions, so the decoration passes the
consistency check, and question 2 is the email question.

## Day boundaries

- **UTC days**: LinkedIn reports daily buckets on UTC days. `fact_creative_daily` keeps each date
  as returned and never rebuckets it. Evidence labels it "LinkedIn days are UTC" next to
  Google's account time zone.
- **Lead instants**: a white-paper lead counts in a window when its `submittedAt` falls in
  `[start 00:00 UTC, end + 1 day 00:00 UTC)`. White-paper spend uses the same UTC days.
- **Display only**: `DISPLAY_TIMEZONE` formats instants for display and never moves LinkedIn's
  day boundary.

## Errors and fallback

The connector raises only `AccessDenied`, `TransientError` or `QueryDefect`
(`connectors/errors.py`). `sync/mode.py` alone decides what follows; the cross-source picture is
in [architecture](../architecture.md).

| Condition | Classification | What happens to LinkedIn |
|-----------|----------------|--------------------------|
| `LINKEDIN_AD_ACCOUNT_IDS` missing, or neither the complete trio nor `LINKEDIN_ACCESS_TOKEN` set | Access unavailable | Simulated, reason "credentials not configured" |
| A 401 after one token refresh, a 401 with a static token, any 403 on any LinkedIn endpoint, or a rejected token exchange | Access denied | All of LinkedIn switches to simulated, with the failing endpoint and LinkedIn's code in the reason |
| 429, 5xx, timeout or network failure | Transient | 3 attempts, then the connection becomes `stale` at once and a failed attempt is recorded on the freshness row of each dataset whose refresh the error failed; that dataset keeps its last good data, and its `last_success_at`, coverage and completeness stand. An entity listing cut short is instead recorded as an `incomplete` read (see Safe absence under [Endpoints](#endpoints)); that read is no successful refresh either, so `linkedin.entities` keeps its `last_success_at` until an entity sync reads every listing. A failed attempt alone flags no item: items reading the dataset are flagged stale once its last success is older than `sources.stale_after_hours` (24) or its coverage stops short of the window end ([P4](../recommendation-rules.md#p3-to-p8)). Never simulated |
| 400 or another 4xx, or a response that breaks its contract | Defect | The job fails and is logged. No retry, no simulation |

- **One source, one mode**: a source's active dataset is entirely live or entirely simulated, so
  a denial on any endpoint switches all of LinkedIn. That includes Lead Sync alone, for example
  when `r_marketing_leadgen_automation` is not granted: simulated leads cannot reference real
  creatives.
- **Reasons**: a reason reads `"{source} {endpoint}: {code or HTTP status}: {message}"`, at most
  500 characters. LinkedIn's code is the body's `code`, or else its `serviceErrorCode`. For
  example:

  ```text
  linkedin lead_form_responses: ACCESS_DENIED: Not enough permissions
  ```

- **Retry policy**: retries back off exponentially from a 2 s base (2 s, then 4 s, each plus up to
  1 s of jitter, at most 30 s). A `Retry-After` of up to 120 s is honoured in full. A longer one
  ends the call at once, so the work is deferred rather than sent early.
- **Partial listings**: a transient failure that survives the retries inside a listing ends the
  listing early. The rows already read are kept, and `complete=False` and `interrupted_by` are
  set.

## Recovery

While LinkedIn is in automatic fallback (`DATA_MODE=auto`, credentials present, access denied),
the worker probes it every 30 minutes. For every configured ad account the probe runs:

- `get_ad_account` (`adAccounts/{id}`);
- a one-day `adAnalytics` for yesterday UTC;
- `leadForms` with `count=1`, one page;
- `leadFormResponses` with `count=1`, one page, over the last 24 hours.

**Outcomes:**

- **All pass**: the worker builds a live LinkedIn dataset and backfills it. Before activation,
  each backfilled lead's HubSpot match is staged and attempted against HubSpot's active primary
  dataset. The dataset activates in one transaction only once the build completes, and a failed
  build is discarded while the simulated dataset stays active.
- **Partial success**: for example, ads readable while Lead Sync still returns 403. The probe
  fails and nothing changes, so LinkedIn never flips back and forth.
- **Forced or unconfigured**: under `DATA_MODE=simulated`, or without credentials, no probe
  request is sent.

## Simulation

When LinkedIn is simulated, `StaticTokenProvider(SIMULATED_TOKEN)` supplies the bearer value and
the simulation's `httpx.MockTransport` answers every call. The live client and parser run
unchanged.

- **Seeded data**: the payloads are seeded by `MOCK_SEED` (default 42), so tests and demos
  reproduce exactly.
- **Live wire shapes**: payloads use LinkedIn URNs with ids in the reserved range
  `990000000–990999999`, `pageToken` and `count`/`start` paging, versioned form URNs with their
  `form` decoration, and lead emails on the reserved `sim.invalid` domain.
- **Contract validation**: requests and responses are validated against the same contract files
  as live traffic, so a request missing a required selector, such as `owner`, fails in simulated
  mode too.
- **Companion CRM dataset**: a simulated LinkedIn dataset is built with a companion simulated
  HubSpot dataset that activates and retires with it. Simulated leads are matched only against
  that companion, and `@sim.invalid` emails never reach a live portal (see [HubSpot](hubspot.md)
  and [architecture](../architecture.md)).

Items computed from simulated LinkedIn data carry the "Simulated data" flag, and the UI names
LinkedIn in its simulated-sources banner with the reason.

## Logging

- **Counts only**: the connector logs one `linkedin.listing` event per operation, with the
  surface, rows, calls, completeness, reason and counts. It never logs a body, element, id list,
  email or token.
- **Redacted URLs**: request URLs are logged only through `redact_url`, which replaces
  credential parameters (`pageToken` included) and any email address.
- **Second line of defence**: the structlog processors also strip `Authorization` values.
- **Clean error messages**: messages are sanitised, so a persisted source-mode reason carries no
  email, secret or token. That covers tokens no setting knows: `send_json` holds the call's own
  `Authorization` token, the form fields of the exchange and the provider's exchanged, rotated
  and retired tokens while each error is built. Every REST call also holds the last 8
  distinct access tokens the client used and, read as the call is sent, every secret the
  token provider holds: the client id, the client secret, the current and rotated refresh
  tokens, and the cached and retired access tokens, or the static access token. No REST request
  carries a refresh token or the client secret, but an error echoing one is stripped of it all
  the same. A platform echoing any of these in plain prose, even a token replaced by an earlier
  refresh or rotation, therefore never reaches `connector.error` or any later reason.
- **Opaque reprs**: `LeadBatch` and `DecodedEmail` hide their contents from `repr`.

## Contract files and references

Each LinkedIn surface has one contract file in `backend/tests/contracts/linkedin/`. A file
records the official reference URL, the API version, the page's last-modified date, the request
shape (path, required query keys and headers, `fields` projection, limits) and the documented
sample. The simulator and the contract tests both read these files.

| Contract | `reference_url` | `api_version` | `last_modified` |
|----------|-----------------|---------------|-----------------|
| `access_token.json` | <https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens> | `202609` (the endpoint itself is unversioned) | 2025-10-08 |
| `ad_account.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-accounts?view=li-lms-2026-09> | `202609` | 2026-09-16 |
| `ad_campaign_groups.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaign-groups?view=li-lms-2026-09> | `202609` | 2026-09-16 |
| `ad_campaigns.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaigns?view=li-lms-2026-09> | `202609` | 2026-07-15 |
| `creatives.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-creatives?view=li-lms-2026-09> | `202609` | 2026-06-24 |
| `posts.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-09> | `202609` | 2026-05-13 |
| `ad_analytics.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting?view=li-lms-2026-09> | `202609` | 2026-06-24 |
| `ad_analytics_statistics.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting?view=li-lms-2026-09> | `202609` | 2026-06-24 |
| `ad_analytics_campaign_delivery.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting?view=li-lms-2026-09> | `202609` | 2026-06-24 |
| `ad_analytics_member_segments.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting?view=li-lms-2026-09> | `202609` | 2026-06-24 |
| `lead_forms.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/lead-sync/leadsync?view=li-lms-2026-09> | `202609` | 2026-07-29 |
| `lead_form_responses.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/lead-sync/leadsync?view=li-lms-2026-09> | `202609` | 2026-07-29 |
| `lead_form_responses_batch.json` | <https://learn.microsoft.com/en-us/linkedin/marketing/lead-sync/leadsync?view=li-lms-2026-09> | `202609` | 2026-07-29 |

The contracts also cite these official pages:

- **Versioning**: every reference above is the version-pinned `view=li-lms-2026-09` page of the
  Marketing API, and each monthly version is supported for at least a year.
- **Reporting**: the Ads Reporting page above, for finders, restrictions and limits, and the
  Reporting schema page,
  <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting-schema?view=li-lms-2026-09>,
  for query parameters, pivot enums and metric types.
- **Account structure**: the accounts, campaign groups, campaigns and creatives pages above.
- **Lead Sync**: the Lead Sync reference, sections "Find Lead Form Responses by Owner" and
  "Get Multiple Lead Form responses"; the Lead Sync schema page; and the Lead Sync API migration
  guide, whose "Response Decoration Migration" example is the `LEAD_FIELDS` projection.

### Live captures

Once live credentials work, record one redacted live exchange per surface from `backend/`:

```bash
cd backend
uv run --frozen ads-engine capture-fixtures --source linkedin
```

- **Refusals**: the command is refused, with nothing sent or written, under
  `DATA_MODE=simulated`, without LinkedIn credentials, or when
  `tests/contracts/linkedin/captured/` is not writable by the user running it, or it or a
  directory on its path below the working directory (`tests`, `tests/contracts` or
  `tests/contracts/linkedin`) is a symbolic link.
- **Bounds**: each surface is bounded to one request. Only the transport retries of a transient
  failure, the token exchange and one retry that follow a 401 in refresh mode, and the
  `adAccounts/{id}` read that `list_campaign_groups` makes when the `ad_account` call failed
  send more. The capture calls the same client methods as sync, with capture-only bounds that
  sync never passes: `max_pages=1` on `list_campaign_groups`, `list_campaigns`,
  `list_creatives` (one campaign), `analytics_daily` and `analytics_segments`, and
  `max_campaigns=1` on `analytics_segments`; lead forms and responses read one page of
  `count=1`. No later `pageToken` or `next` link is followed. A bounded listing ends incomplete
  with the reason "page limit" only when the bound leaves work unread: its page still carries a
  next page token or a `next` link, its `count=1` Lead Sync page holds an element (more may
  follow), or the segment fallback found more delivering campaigns than `max_campaigns`. A
  search page without a next page token, an `adAnalytics` response without a `next` link and
  an empty Lead Sync page end the listing complete, except that an `adAnalytics` response at
  the 15,000-element cap ends it incomplete with its result-limit reason (see
  [Limits and retention](#limits-and-retention)).
- **Segment fallback**: when LinkedIn rejects the Statistics Finder with HTTP 400, the segment
  call sends one `ad_analytics_campaign_delivery` discovery request and one
  `ad_analytics_member_segments` request for the first delivering campaign. Both are captured
  under their own surfaces, and `ad_analytics_statistics` is then not captured, because its
  only response was the 400.
- **Output**: it writes `tests/contracts/linkedin/captured/{surface}.json`, each shaped
  `{surface, captured_at, request, response}`. `access_token` is captured only in refresh mode,
  and `lead_form_responses_batch` only when the one lead response read (`count=1`, the last
  30 days) is a sponsored lead. A test lead is never re-fetched; the skip reason then says so,
  rather than reporting that the window holds no sponsored lead.
- **Redaction**: every capture is redacted before it is written, with the marker `"<redacted>"`.
  That covers:
  - `Authorization` values, with the scheme kept;
  - `access_token`, `refresh_token`, `client_id` and `client_secret` values, and `code` in
    requests;
  - person-name and contact keys: `firstName`, `lastName`, `firstname`, `lastname`, `fullName`,
    `phone`, `mobilephone` and `email`;
  - every `formResponse.answers[].answerDetails` object, whole;
  - any email address, and every configured secret, `LINKEDIN_CLIENT_ID` included, in any
    string whatever its length, in plain text or in any percent-encoded spelling.

  The access token the token exchange issues, any refresh token it rotates to and every
  `Authorization` credential sent are redacted the same way wherever they occur, such as in
  the token response's `scope`, a key, a percent-encoded string or a later response.
  Configured ad account ids become zeros of the same length, and so does the id of every
  organization URN (`urn:li:organization:`, `urn:li:organizationBrand:` and `urn:li:company:`,
  plain or percent-encoded), such as an account's `reference`, a campaign group's
  `beneficiaryReference`, a campaign's `associatedEntity` and employer targeting, and a post's
  `author`. The URN prefix is kept, and a bare number equal to an organization id is never
  changed. A capture that still holds a configured secret or one of these tokens, however many
  times it was percent-encoded, is not written, and the command exits with status 1. The
  `LEAD_FIELDS` projection does not request `submitter`, the member URN, so captures of lead
  responses do not carry it.
- **Use in tests**: the contract tests then validate the parsers against the captured payloads
  as well as the documented samples.
