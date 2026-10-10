# HubSpot CRM

The HubSpot connector reads the CRM so that recommendations are judged against real pipeline,
not only platform-reported leads. It reads the contacts behind LinkedIn leads, their MQL
("First meeting booked") and Qualified Lead stage dates, their meetings and deals, and the
associations between them. Optionally, it also reads contacts touched by Google Ads.

The connector is read-only. It sends batch reads, CRM searches and property `GET` requests.
It never creates, updates or deletes anything in HubSpot, because the product only recommends.

This page is for two readers:

- an operator who creates the HubSpot token and binds the portal's lifecycle stages;
- a developer who changes the connector or lead matching.

Related pages:

- [recommendation-rules.md](../recommendation-rules.md): how MQL, QL, meetings and deals feed
  the rules, floors and the pipeline veto.
- [architecture.md](../architecture.md): scheduler, datasets, source modes and fallback.
- [runbook.md](../runbook.md): token creation, scopes, simulated mode and recovery steps.
- [linkedin.md](linkedin.md): the LinkedIn side of matching (lead responses, email re-fetch,
  form versions).
- [README](../../README.md): quick start.

## Code map

Paths are relative to `backend/src/ads_engine/`.

| Module | Responsibility |
|--------|----------------|
| `connectors/hubspot/client.py` | `HubSpotClient`: every HubSpot call, the 4 requests/s throttle, binding verification, the recovery probe |
| `connectors/hubspot/search.py` | Partitioned CRM Search below the 10,000-result ceiling |
| `connectors/hubspot/parser.py` | Response decoding, typed contact, meeting and deal rows, email normalisation and SHA-256 |
| `sync/lead_match.py` | Durable lead-to-contact matching and its retry schedule |
| `sync/jobs.py` (`CrmSyncJob`) | The CRM sync of one HubSpot dataset, primary or companion |
| `sync/mode.py`, `sync/datasets.py` | Source mode, fallback, probes, staged builds, readiness and atomic activation |
| `analysis/thresholds.py` | Loads `config/hubspot_mapping.yaml` (`HubSpotMapping`) and the stored `CrmBindings` |

## API version and support

- Every call uses its surface's dated `2026-09` path (`HUBSPOT_API_VERSION`, pinned in
  `settings.py` and consumed by `client.py` and the simulator's surface router).
  No numbered path (`/crm/v3/…`, `/crm/v4/…`) is used anywhere, and the connector's
  integration tests assert that none is sent.
- HubSpot's changelog "Introducing date-based API versioning" (2026-03-30) states the support
  policy. Each date version is immutable once released and is supported for at least
  18 months: 6 months as current, then 12 months with critical fixes only. Support applies to
  the whole version, not endpoint by endpoint.
- `2026-09` shipped on 2026-09-08, so it is supported until at least March 2028.
- HubSpot has announced that v4, including `/crm/v4/associations/…`, becomes unsupported on
  2027-03-30, and v1 to v3 in September 2027. The `2026-09` associations surface performs the
  same batch read, so the product never depends on a numbered contract.
- A version move changes two places together: the `HUBSPOT_API_VERSION` pin in `settings.py`,
  which `client.py` and `simulation/payloads.py` read, and each contract file's `request.url`
  and `api_version`. The contract tests then rerun against the new reference pages.

## Credentials and settings

| Variable | Default in `.env.example` | Meaning |
|----------|---------------------------|---------|
| `HUBSPOT_ACCESS_TOKEN` | empty (secret) | A Service Key (account level) or legacy private-app token, sent as `Authorization: Bearer <token>`. Held as a `SecretStr`, so it never prints |
| `HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY` | empty | Optional contact property holding the Google campaign id ([Google-to-CRM link](#optional-google-to-crm-link)) |
| `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY` | empty | Optional `datetime` contact property holding when that Google touch happened; a `date` property leaves the link unavailable |
| `DATA_MODE` | `auto` | `auto` uses live HubSpot when the token works and simulated data otherwise; `simulated` forces simulated data and never probes |
| `MOCK_SEED` | `42` | Seed of the simulated portal |
| `BACKFILL_DAYS` | `180` | CRM scope in days: the lead staging range and the backfill deal search, capped at 180 |

- The token needs read scopes only, for contacts, deals and meetings. The runbook covers
  creating it and granting scopes ([runbook.md](../runbook.md)).
- A missing scope returns 403 `MISSING_SCOPES`. That is access denied: HubSpot switches to
  simulated data, and the mode reason names the platform code and the failing endpoint
  ([Errors, fallback and recovery](#errors-fallback-and-recovery)).
- The token lives only in the server-side `.env`, which is gitignored and read by the
  `migrate`, `api` and `worker` containers. It never reaches the repository, the browser
  bundle or the logs.
- The product reads only the name `HUBSPOT_ACCESS_TOKEN`. A token stored under another name
  must be copied into `.env` under this one.
- A blank value is read as unset. With no token, HubSpot runs on simulated data with the
  reason `credentials not configured`.

## Throttle, timeouts and retries

- **Throttle**: every attempt, retries included, first waits on the client's rate limiter.
  `SHARED_LIMITER` (4 requests per second) is the default of every `HubSpotClient` in the
  process, so total HubSpot traffic stays at 4 per second however many clients exist.
  - Consecutive starts are at least 0.25 s apart.
  - Any five starts span at least 1.05 s (`WINDOW_GUARD_SECONDS` adds 50 ms), so a request
    delayed up to 50 ms between grant and send still leaves at most four in any one-second
    window.
  - This stays below HubSpot's CRM Search limit of 5 requests per second per account and the
    private-app burst limit of 100 to 190 requests per 10 s.
  - The throttle bounds sync duration. For example, a backfill's deal search makes at least
    one call per one-day partition.
- **Exceeding a HubSpot limit** returns 429 `RATE_LIMIT`, which is transient.
- **Timeouts**: each attempt has a 60 s deadline from send to the last byte of the body, and
  a 10 s connect timeout (`SOURCE_TIMEOUTS`, `CONNECT_TIMEOUT_SECONDS` in `connectors/http.py`).
- **Retries** (`DEFAULT_RETRY` in `connectors/http.py`): 3 attempts in total, exponential
  backoff from a 2 s base (2 s, then 4 s, each plus up to 1 s of jitter, capped at 30 s).
  A `Retry-After` header replaces the backoff and is honoured in full up to 120 s. A longer
  requested wait ends the call at once with a transient error, so nothing is sent before
  HubSpot allows it.

## Surfaces

All paths are relative to `https://api.hubapi.com`. Every request carries
`Authorization: Bearer <token>` and `Accept: application/json`, plus
`Content-Type: application/json` on a `POST`.

| Surface (contract file) | Method and path | Limits | Used for |
|-------------------------|-----------------|--------|----------|
| `objects_batch_read` | `POST /crm/objects/2026-09/{objectType}/batch/read` | At most 100 inputs per call; answers 200 or 207 | Contacts by `idProperty=email` (lead matching); contacts by id (contact refresh, and the contacts associated with searched deals); meetings and deals by id (target reads); the probe |
| `crm_search` | `POST /crm/objects/2026-09/{objectType}/search` | `limit` at most 200 per page; at most 10,000 results per query, and paging past them returns 400; request body under 3,000 characters; at most 5 filter groups, 6 filters per group and 18 filters in total; 5 requests per second per account | The window deal search; Google-touch contact discovery; the probe |
| `associations_batch_read` | `POST /crm/associations/2026-09/{fromObjectType}/{toObjectType}/batch/read` | At most 1,000 inputs per call; answers 200 or 207 | Contacts to meetings, contacts to deals, deals to contacts; the probe |
| `properties` | `GET /crm/properties/2026-09/{objectType}/{propertyName}` | One property per call; 404 means the property does not exist | Startup binding verification |

- The client checks a CRM Search body before sending it. It rejects, as a defect, a body of
  3,000 characters or more (compact JSON), a `limit` outside 1 to 200, and an `after` cursor
  at or past the 10,000-result ceiling, because HubSpot answers each with 400.
- A batch `status` other than `COMPLETE` (`CANCELED`, `PENDING` or `PROCESSING`) is a failed
  call, even when the body is otherwise valid.

A lead-matching lookup, as the client sends it (the property list is the base set plus the
bound stage-date and Google properties, [Contact refresh](#contact-refresh)):

```http
POST /crm/objects/2026-09/contacts/batch/read HTTP/1.1
Host: api.hubapi.com
Authorization: Bearer <token>
Accept: application/json
Content-Type: application/json

{"idProperty": "email", "inputs": [{"id": "<email>"}], "properties": ["email", "hs_additional_emails", "lifecyclestage", "lastmodifieddate", "<MQL date property>", "<QL date property>"]}
```

## Startup property verification and stage binding

Portals customise lifecycle stages, so MQL and QL are bound in `config/hubspot_mapping.yaml`
and resolved against the portal's own property definitions. Nothing about a stage is assumed.

Verification (`HubSpotClient.verify_bindings`) runs:

- at worker startup, for HubSpot's active primary dataset and for the companion of a
  simulated active LinkedIn dataset;
- on every build of a HubSpot dataset or companion, against that dataset's own portal;
- in the CRM sync, when a dataset has no bindings stored yet.

A live portal and a simulated one resolve different stage ids, so each HubSpot dataset keeps
its own bindings, and they retire with it. A transient failure during re-verification keeps
the bindings already stored.

### Configuration

The shipped defaults, from `config/hubspot_mapping.yaml` with its comments removed:

```yaml
mql:
  mode: stage_label
  stage_label: "First meeting booked"
  property: null

ql:
  mode: stage_label
  stage_labels: ["Qualified Lead", "Sales Qualified Lead"]
  property: null

crm:
  booking_sources: [MEETINGS_PUBLIC]
  meeting_outcome_exclusions: [CANCELED]
```

- `mode` is `stage_label` or `property`. A binding sets `stage_label` (one label) or
  `stage_labels` (a list tried in order), never both. Unknown keys fail config load.
- The `crm` map here is merged with the `crm` map of `config/thresholds.yaml`, which holds
  `crm.lead_maturity_days` (14) and `crm.veto_min_leads` (20). A key defined in both files
  fails config load.
- The Google-to-CRM link properties are environment variables, not keys in this file.
- Changing the mapping takes effect when the `api` and `worker` restart: the API reads the
  configuration once per process, and the worker re-verifies the bindings at startup. The
  file feeds `config_hash`, so later runs record the change.

### Resolution rules

In `stage_label` mode, verification first reads the stage options:

```http
GET /crm/properties/2026-09/contacts/lifecyclestage HTTP/1.1
Host: api.hubapi.com
Authorization: Bearer <token>
Accept: application/json
```

Each option carries a label and an internal value. Default stages have internal names such
as `marketingqualifiedlead`; custom stages have numeric ids. An excerpt of the shape, with
placeholders:

```json
{
  "name": "lifecyclestage",
  "type": "enumeration",
  "options": [
    {"label": "<stage label>", "value": "<stage id>", "hidden": false, "displayOrder": 0}
  ]
}
```

- **`stage_label` mode**: the configured labels are tried in order against the options.
  - A label matches an option whose label is equal after trimming and case-folding. An exact
    match is preferred, then a visible option over a hidden one.
  - The first configured label that matches wins. For QL that is a custom "Qualified Lead"
    stage first, then HubSpot's default "Sales Qualified Lead".
  - The count reads that stage's "date entered" property, `hs_v2_date_entered_{stage id}`:
    for example `hs_v2_date_entered_marketingqualifiedlead` for a default stage, or
    `hs_v2_date_entered_<numeric id>` for a custom one. A second properties `GET` must
    confirm that it exists.
  - If that property is missing, the binding is unavailable. A later label never stands in.
  - `lifecyclestage` is read at most once per verification.
- **`property` mode**: `property` names a contact property that records the event directly,
  for example a custom "first meeting booked" date. It must exist and have type `date` or
  `datetime`. Labels are kept for display only.
- **Unresolved means unavailable, never zero**: the metric is `unavailable` and shows as
  "unavailable", never as 0. The stored reason names the missing stage or property, and the
  run reports a finding. The reasons the client writes are:
  - `lifecyclestage property not found`
  - `stage 'First meeting booked' not found in lifecyclestage options`
  - `stages 'Qualified Lead', 'Sales Qualified Lead' not found in lifecyclestage options`
  - `stage '<label>' resolved to id '<stage id>', but property 'hs_v2_date_entered_<stage id>' not found`
  - `property '<name>' not found on contacts`
  - `property '<name>' has type '<type>', not a date or datetime property`
- **Unresolved QL**: the pipeline is unavailable. The C2 veto is not evaluated, its items
  carry `pipeline_unavailable`, and C1b and W2 do not fire.
- **No assumed meaning**: HubSpot's default "Marketing Qualified Lead" stage is never assumed
  to mean "First meeting booked". MQL uses it only when the configuration names it.
- **Independence**: QL is resolved and observed from its own property, never derived from
  MQL. MQL and QL are shown side by side and never blended in any field, total or sort key.
- **Skipped stages**: an empty stage-date property is never counted, even when the contact's
  current lifecycle stage is later. No other date stands in, so every count is an observed
  stage entry. Only some of these contacts are reported as skipped: those whose latest refresh
  succeeded (`contact_read_status` is `ok` on every matched `lead_match` row of the contact)
  and whose current `lifecyclestage` is set and is neither `subscriber` nor `lead`, the entry
  stages that precede every bound stage. The `skipped_stage` finding reports them per stage,
  for example "MQL (First meeting booked): N contacts skipped this stage (not counted)". A
  contact still at `subscriber` or `lead` (awaiting qualification), with no lifecycle stage,
  or whose refresh failed or was never recorded is not counted and not reported as skipped.
  Without a successful refresh its current stage is not known, and its CRM evidence is
  unknown instead.
- **Causal order**: a bound date counts only at or after the contact's touch with the content
  (the lead's `submittedAt`). Earlier entries are reported as "already MQL/QL before this
  touch".

How the counts feed the rules, floors and the pipeline veto is in
[recommendation-rules.md](../recommendation-rules.md).

### Where the resolved bindings appear

The result, `CrmBindings`, is stored on the HubSpot dataset as `source_dataset.crm_bindings`.
Attribution and the API read it from there without calling HubSpot. Its shape, with an
unresolved QL binding and no Google link as the example:

```json
{
  "mql": {
    "available": true,
    "property": "hs_v2_date_entered_<stage id>",
    "stage_id": "<stage id>",
    "label": "First meeting booked",
    "reason": null
  },
  "ql": {
    "available": false,
    "property": null,
    "stage_id": null,
    "label": null,
    "reason": "stages 'Qualified Lead', 'Sales Qualified Lead' not found in lifecyclestage options"
  },
  "google": {
    "available": false,
    "campaign_property": null,
    "touch_date_property": null,
    "reason": "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured"
  },
  "verified_at": "<ISO-8601 instant in UTC>"
}
```

- `GET /api/v1/config` returns `hubspot_mapping` (as configured), `crm_bindings` (resolved
  against HubSpot's active primary portal; `null` until verified) and
  `companion_crm_bindings` (the active companion's; `null` when there is none). The
  `/settings` screen shows them read-only.
- The white-paper table heads its MQL column "MQL ({label})". The label is that of the MQL
  binding the run computed its counts with, in the effective CRM dataset (the companion while
  LinkedIn is simulated). It is frozen in each row's evidence, so a later binding change never
  relabels an earlier run.
  - `stage_label` mode: the matched lifecycle-stage option's label, for example
    "MQL (First meeting booked)", or the configured label while no option matches.
  - `property` mode: the configured label kept for display, else the bound property name, for
    example "MQL (date_first_meeting_was_booked)".
  - Only a table none of whose rows records a label takes the current one from
    `GET /api/v1/config`, chosen the same way from `companion_crm_bindings` (else
    `crm_bindings`) and `hubspot_mapping`. While that read loads or fails, the header is "MQL".
  - The header is plain "MQL" when a table's rows record different labels (each row keeps its
    own in its details), or when the label is blank or only repeats "MQL".
- Contact reads request only the stage-date properties of available bindings. An unavailable
  count therefore reads as unavailable, never as a skipped stage.

## Lead-to-contact matching

Every LinkedIn lead is matched to a HubSpot contact once per CRM dataset. The state of that
match is durable, so a contact created late, or a CRM that was simulated when the lead
arrived, is still matched later. No email is ever stored.

`lead_match` holds one row per (lead, CRM dataset), with primary key
`(lead_id, crm_dataset_id)`. A CRM dataset being built therefore stages its own matches
without touching the active dataset's. Its columns:

- `status`: `pending`, `matched`, `no_match`, `no_email` or `error` (below);
- `attempts`, `next_attempt_at` and `last_checked_at`;
- `contact_id`: the matched contact, a contact in `crm_dataset_id`;
- `contact_read_status`: `ok` or `error` from the latest contact refresh, `null` until
  matched ([Contact refresh](#contact-refresh));
- `reason`: why the row is not matched.

| Status | Meaning | Retried |
|--------|---------|---------|
| `pending` | Staged and not yet attempted; due from its `next_attempt_at` (the staging instant), or at once when that is empty, but not while a claim's lease holds it, until the lease expires | Not yet attempted |
| `matched` | A contact of `crm_dataset_id` was found and `contact_id` is set; `contact_read_status` starts `ok`, and each later [contact refresh](#contact-refresh) sets it to `ok` or `error` without changing the match | No (terminal) |
| `no_match` | HubSpot has no contact with this email yet | Yes |
| `error` | The attempt got no answer, or failed terminally (outcomes below) | Only when the failure was transient |
| `no_email` | The lead response carries no decodable email | No (terminal) |

Every status other than `matched` counts as unresolved in attribution. Every status other
than `pending` counts as attempted for the staged-matching readiness test.

### Matching flow

1. **LinkedIn lead job**: normalises each lead's email (trimmed, lowercased), stores only its
   SHA-256 (`lead.email_sha256`) and discards the plaintext. Each new lead gets a `pending`
   row in its effective CRM dataset: the companion while LinkedIn is simulated, HubSpot's
   active primary otherwise. A live lead also gets a row in every `building` HubSpot primary.
2. **Claim**: the HubSpot CRM job takes up to 100 due rows (`BATCH_SIZE`) in one short
   transaction, in due-time order whatever their status: `next_attempt_at` ascending, empty
   first, then `lead_id`. A retry due before a `pending` row's staging instant therefore
   comes ahead of it. It selects them `FOR UPDATE OF lead_match SKIP LOCKED`, so concurrent
   runs take disjoint rows, leases them by writing the lease instant to `next_attempt_at`
   (`CLAIM_LEASE`, 30 min), and commits.
3. **Re-fetch**, with no transaction or row lock held: the job re-reads the lead responses
   from LinkedIn by plain response id and decodes each email with the response's own
   form-version map ([linkedin.md](linkedin.md)).
4. **Look up**: one `contacts_by_email` call sends the decoded emails to
   `POST /crm/objects/2026-09/contacts/batch/read` with `idProperty=email`, at most 100 per
   call. The plaintext is dropped once the call returns.
5. **Persist**, in a second short transaction: each row still under its lease gets its
   outcome, one more attempt, `last_checked_at` and its next due time. Only the contacts
   found for those rows are upserted into `crm_contact`, with the email kept as its SHA-256
   only. Commit.

Emails exist only in memory for the lookup that needs them. They are never stored, logged or
placed in an exception, and the lookup's result is keyed by SHA-256.

Restart safety:

- An exception or cancellation between claim and persist hands the lease back, so the rows
  are due again unchanged.
- A process that dies mid-batch leaves its lease to expire, after which the rows are due
  again.
- Batches already committed stand, so a crash mid-job loses no state.
- A row whose lease expired and was claimed by another run belongs to that run and is
  skipped, so its newer state is never overwritten.

### Outcomes

| Outcome | `status` | `reason` | Retried |
|---------|----------|----------|---------|
| A contact's primary email or one of its `hs_additional_emails` equals the lead's email | `matched` | — | No |
| The 207 response lists the email under category `OBJECT_NOT_FOUND` | `no_match` | `no HubSpot contact with this email yet` | Yes |
| Another error category, a batch status other than `COMPLETE`, an email neither returned nor listed, or a call that still failed after its retries | `error` | `HubSpot contact lookup failed` | Yes (transient) |
| A transient LinkedIn failure left the response unread | `error` | That failure's reason | Yes (transient) |
| LinkedIn no longer returns the response | `error` | `email no longer retrievable` | No |
| The re-fetched response fails the Lead Sync contract | `error` | `re-fetched lead response failed contract validation` | No |
| A simulated `@sim.invalid` email met a live HubSpot client | `error` | `simulated email withheld from live portal` | No |
| The lead and the CRM dataset are not a permitted pair | `error` | `lead dataset is not paired with this CRM dataset` | No |
| An answered question id is missing from the response's own form version | `no_email` | `form map mismatch` | No |
| The response has no form decoration and no stored map for its version | `no_email` | `unknown form version` | No |
| No email question carries a usable address | `no_email` | `no email answer` | No |

- An explicit failure for an email outranks an explicit "not found".
- A live client never sends an address on the reserved simulated domain `sim.invalid`. The
  lookup reports such an address as skipped, and the row ends as the terminal `error` above.

### Retry schedule

- A retried row waits 1 h, 6 h and 24 h after its first, second and third attempts
  (`RETRY_DELAYS`), then 1 day after every later attempt.
- Retries stop when the next attempt would fall later than submission plus 30 days
  (`MATCH_HORIZON`). The row keeps its status with no `next_attempt_at` and stays unresolved.
  Contacts created up to 30 days after the lead are therefore still found.
- A transient failure is no answer at all, so it keeps the same schedule past the horizon
  until 10 attempts in total (`TRANSIENT_ATTEMPT_LIMIT`). Backfilled leads staged for a new
  CRM dataset are often older than 30 days at their first attempt, and one outage would
  otherwise leave them unresolved for good.
- `no_email` and the terminal `error` reasons are never retried.

Unresolved leads stay in every attribution population and are never dropped. When their
share reaches `attribution.floor_share` (default `0.10`), white-paper counts become floors and
the pipeline veto becomes unavailable ([recommendation-rules.md](../recommendation-rules.md)).

### Staged matching on a CRM dataset change

A new HubSpot dataset (a live dataset after recovery) and a new companion dataset are matched
while they are still `building`. The previous dataset's matches stay active and in use
throughout.

- **Rows staged**:
  - a HubSpot primary build stages a `pending` row for every lead of the active LinkedIn
    dataset in the backfill range, when that LinkedIn dataset is live (simulated leads are
    matched only in their companion);
  - a companion build stages its parent's leads;
  - a new live LinkedIn dataset stages its backfilled leads against HubSpot's active primary
    before it activates, so LinkedIn-attributed CRM evidence is complete from the first run
    on the new dataset.
- **Work in the building dataset**: matching, contact refresh, associations with their
  target reads, the deal search and Google-contact discovery.
- **Readiness** (`DatasetManager.readiness`):
  - every staged row has been attempted at least once (status other than `pending`);
  - every required freshness row of the building dataset is `complete`, or `incomplete` with
    a recorded reason, and never `unknown`. The required rows are `hubspot.contacts`,
    `hubspot.associations` and `hubspot.deal_search`, plus `hubspot.google_contacts` for a
    primary whose Google link is verified;
  - the backfill completed, as for every source: no required row is `incomplete` with unread
    work (an `interrupted` read), and the latest attempt of each required row succeeded. A
    permanent shortfall, such as a search partition read up to its result ceiling, is
    accepted;
  - for a simulated LinkedIn dataset, its companion is ready too.
- **Activation** (`DatasetManager.activate`): one transaction, serialised on the source's
  `source_connection` row, activates the building dataset (with its companion) and retires
  the previous one with its matching state. No lead is reset to `pending`, and active
  matching state is never deleted or requeued.
- If LinkedIn activates a new dataset during a HubSpot build, the build stages rows for the
  new leads before it can become ready.
- If HubSpot activates a new primary during a live LinkedIn build, the build stages its leads
  there and matches them. When any are matched, it runs that primary's CRM sync, so the new
  matches get their contact refresh, associations and target reads before LinkedIn
  activates. The activation lock refuses a LinkedIn build whose matched contacts there still
  have no association read, and a build whose contacts still lack them after three settle
  rounds is discarded.
- Leads ingested after activation get rows in the active dataset as usual.

## Contact refresh

Each CRM sync re-reads by id every matched contact of its dataset and, when Google-touch
discovery runs, every stored Google-touch contact whatever its touch date, with
`POST /crm/objects/2026-09/contacts/batch/read` (no `idProperty`, at most 100 per call). A
Google-only contact touched before the current discovery range is no longer rediscovered, so
only this read keeps its stage dates current.

- **Properties requested**: `email`, `hs_additional_emails`, `lifecyclestage`,
  `lastmodifieddate`, the stage-date properties of the available MQL and QL bindings, and
  the two Google properties when the Google link is verified and the settings still name
  them.
- **Stored**: the email as its SHA-256 only, the lifecycle stage, the MQL and QL dates
  (`mql_at`, `ql_at`) and the Google campaign and touch values.
- **Read status**: each matched row's `contact_read_status` becomes `ok`, or `error` when the
  contact was absent from the response, listed in `errors[]`, or in a call that still failed
  after its retries. A record returned under an id that was not requested, such as a merged
  contact's new id, counts as not read. A Google-only contact has no `lead_match` row and
  carries no per-contact status; one not read makes `hubspot.google_contacts` incomplete
  ([Google-to-CRM link](#optional-google-to-crm-link)).
- **On `error`**: the previous stage values are kept but treated as unknown for the window.
  The contact's CRM evidence is then unknown, never zero, and `hubspot.contacts` is recorded
  `incomplete` with the reason "N contacts not read", counting matched and Google-touch
  contacts alike.
- **Interrupted refresh**: when a call still failed after its retries, the contacts read are
  stored and the others keep their previous values, but `hubspot.contacts` records a failed
  attempt instead of a success, and so does `hubspot.google_contacts` when a Google-touch
  contact went unread ([Errors, fallback and recovery](#errors-fallback-and-recovery)).
- A contact read requires a bound property map. Without one the client raises a defect
  (`CONTACT_PROPERTIES_UNBOUND`) before any call, because an empty map would read every bound
  date as absent and count the stage as skipped instead of unavailable.

## Associations

Associations are read with `HubSpotClient.associations` on three traversals:

| From | To | Inputs |
|------|----|--------|
| `contacts` | `meetings` | Matched contacts and Google-touch contacts |
| `contacts` | `deals` | Matched contacts and Google-touch contacts |
| `deals` | `contacts` | Every deal the deal search found |

### Requests and continuation

The initial call sends only ids, at most 1,000 inputs per call:

```json
{"inputs": [{"id": "<contact id 1>"}, {"id": "<contact id 2>"}]}
```

Each input whose result returned `paging.next.after` is re-sent with its own cursor, batched
at most 1,000 per call, until no paging object comes back:

```json
{"inputs": [{"id": "<contact id 1>", "after": "<paging.next.after of contact id 1>"}]}
```

- Inputs whose results came back without paging are never re-sent.
- A cursor is never sent on another input. `paging.next.link` is ignored.
- A cursor that repeats for the same input ends that input as `partial` (or `error` when
  nothing was read) with "paging cursor did not advance", which bounds the loop.

### Responses

- **200**: `{status, results[], startedAt, completedAt}`. Each result is
  `{from: {id}, to: [{toObjectId, associationTypes[]}], paging?: {next: {after}}}`.
  `paging` is present only when the input has more pages.
- **207**: the same body plus `errors[]` and `numErrors`. A failed input is absent from
  `results` and named in its error entry's `context`.
- A `status` other than `COMPLETE` means the whole call failed, for every input it carried.
- `toObjectId` may arrive as a string or a number; the parser normalises it to a string.

An illustrative 207 page, shaped as the parser reads it. The first contact has a meeting and
more pages; the second has no meetings at all:

```json
{
  "status": "COMPLETE",
  "results": [
    {
      "from": {"id": "<contact id 1>"},
      "to": [
        {
          "toObjectId": "<meeting id>",
          "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 199, "label": null}]
        }
      ],
      "paging": {"next": {"after": "<cursor>"}}
    }
  ],
  "errors": [
    {
      "status": "error",
      "category": "OBJECT_NOT_FOUND",
      "subCategory": "crm.associations.NO_ASSOCIATIONS_FOUND",
      "message": "<message>",
      "context": {"fromObjectId": ["<contact id 2>"]}
    }
  ],
  "numErrors": 1,
  "startedAt": "<ISO-8601 instant>",
  "completedAt": "<ISO-8601 instant>"
}
```

The reference page's own rendered 207 example, verbatim from `documented_sample` in
`backend/tests/contracts/hubspot/associations_batch_read.json`. Its `CANCELED` status makes it
a failed call, so the client settles none of its inputs from it:

```json
{
  "completedAt": "2023-11-07T05:31:56Z",
  "results": [
    {
      "from": {
        "id": "<string>"
      },
      "to": [
        {
          "associationTypes": [
            {
              "category": "HUBSPOT_DEFINED",
              "typeId": 123,
              "fromObjectTypeId": "<string>",
              "label": "<string>",
              "toObjectTypeId": "<string>"
            }
          ],
          "toObjectId": "<string>"
        }
      ],
      "paging": {
        "next": {
          "after": "<string>",
          "link": "<string>"
        },
        "prev": {
          "before": "<string>",
          "link": "<string>"
        }
      }
    }
  ],
  "startedAt": "2023-11-07T05:31:56Z",
  "status": "CANCELED",
  "errors": [
    {
      "category": "<string>",
      "context": {},
      "errors": [
        {
          "message": "<string>",
          "code": "<string>",
          "context": "{missingScopes=[scope1, scope2]}",
          "in": "<string>",
          "subCategory": "<string>"
        }
      ],
      "links": {},
      "message": "<string>",
      "status": "<string>",
      "id": "<string>",
      "subCategory": {}
    }
  ],
  "links": {},
  "numErrors": 123,
  "requestedAt": "2023-11-07T05:31:56Z"
}
```

### "No associations" is a genuine zero (plan correction)

The plan reads every input listed in a 207 `errors[]` as `error`. HubSpot, however, reports
an input that simply has no associations as exactly such an entry: category
`OBJECT_NOT_FOUND`, subcategory `crm.associations.NO_ASSOCIATIONS_FOUND`
(`NO_ASSOCIATIONS_FOUND` in `parser.py`), with the input in `context.fromObjectId`.

- The connector records that entry as `ok_empty`, a genuine zero. A category ending in
  `NO_ASSOCIATIONS_FOUND` is recognised as the same case.
- Every other error entry is `error`.
- Without this correction, every contact without meetings or deals would be unknown
  evidence, and W2 and C1b would be withheld permanently, while the attribution rules require
  `ok_empty` to be the genuine zero.

### Fetch status

Every (object, target type) read is recorded in `crm_association_fetch`, with the columns
`object_type`, `object_id`, `to_type`, `status`, `target_read_status`, `missing_target_ids`,
`fetched_at` and `error`. `status` is one of:

| `status` | When |
|----------|------|
| `ok` | Targets were read and every continuation succeeded |
| `ok_empty` | No associations: an empty `to` with no further page, or the `NO_ASSOCIATIONS_FOUND` entry above |
| `partial` | A continuation failed (after its retries, through an error entry, a status other than `COMPLETE` or a repeated cursor) after some targets were read |
| `error` | Any other error entry; an input in neither `results` nor `errors`; an initial call that still failed after its retries or returned a status other than `COMPLETE`; a continuation that failed before any target was read |

## Target reads and known evidence

The associated records are then batch read by id, at most 100 per call:

- **Meetings** (`MEETING_PROPERTIES`): `hs_createdate`, `hs_timestamp`,
  `hs_meeting_start_time`, `hs_meeting_outcome`, `hs_meeting_source`. `hs_meeting_end_time`
  is not requested, because `crm_meeting` has no column for it.
- **Deals** (`DEAL_PROPERTIES`): `createdate`, `dealstage`, `pipeline`, `amount`. Deals the
  deal search already read are not read again.
- **Contacts** associated with searched deals, through the contact refresh read. Contacts
  already known are not read again.

Each fetch row records `target_read_status`:

| `target_read_status` | When |
|----------------------|------|
| `ok` | Every associated id was returned; also an `ok` fetch that yielded no target id, so there was nothing to read |
| `partial` | Some ids were missing, or one of the calls failed |
| `error` | None of the ids were read |
| `null` | Nothing to read: the fetch is `ok_empty` (a known zero), or it is `partial` or `error` and collected no target id (unknown) |

`missing_target_ids` lists every associated id that was not read, whether HubSpot omitted it
or its call failed.

- **Known evidence**: an association is known in exactly two cases: its fetch is `ok_empty`,
  whose target reads are `null`; or its fetch is `ok` and its target reads are `ok`. Anything
  else is unknown: a `partial` or `error` fetch whatever its target reads, `null` included, and
  an `ok` fetch whose target reads are `partial`, `error` or `null`. An empty successful result
  is a genuine zero.
- **Unknown, never absent**: an associated but unread meeting or deal is never treated as
  absent. A failed or partial step on a contact's meetings or deals makes that contact's
  evidence unknown, which withholds W2 and C1b and makes the C2 veto unavailable.
- **Orphan deals**: a deal whose deals-to-contacts fetch is `ok_empty` is an orphan; one whose
  fetch is `ok` with target reads `ok` is linked; anything else is unknown and excluded from
  the assessed scope.

## Meetings and deals: event times

- **No booked-at field**: HubSpot records no booking instant. Booking time is proxied by the
  meeting record's creation (`hs_createdate`), labelled "booked (record created)".
- **Booking sources**: the proxy equals the booking instant only for meetings the booking
  itself created. `crm.booking_sources` therefore defaults to `[MEETINGS_PUBLIC]`, a
  scheduling-page booking (`hs_meeting_source`).
- **Booked after touch**: a meeting counts only when its creation and its
  `hs_meeting_start_time` are both at or after the contact's touch, and its outcome is not in
  `crm.meeting_outcome_exclusions` (default `[CANCELED]`). When `hs_meeting_start_time` is
  empty, `hs_timestamp` stands in as the start.
- **Other sources**: meetings imported, synced from a calendar or logged by hand can be
  created after the real booking. They pass the same time checks but go to "Other meetings
  (ordering unverified)", carry the `meeting_time_proxy` flag and are excluded from cost per
  meeting booked.
- **Deals**: `createdate` is the deal's creation time, exactly the event the causal check
  names. A deal counts only when it was created at or after the contact's touch.

The full causal check, credit and floors are in
[recommendation-rules.md](../recommendation-rules.md).

## Partitioned deal search

The window deal search reads every portal deal created from window start to run time, so the
orphan-deal share is measured over all of them, not only over deals reached from matched
contacts. CRM Search stops at 10,000 results per query, so `search.search_partitioned` reads
the range as a sequence of narrower queries whose union is the range.

- **Range**: `createdate` from the default window start to the run time. During a dataset's
  backfill the start is the backfill start (`BACKFILL_DAYS`, at most 180 days). When the
  stored `hubspot.deal_search` row is `incomplete` for a repairable reason
  (`freshness.needs_repair`), the start widens back to that row's coverage start, so the gap
  is read again. A transient failure records a failed attempt instead (step 6), which leaves
  the stored row's coverage and completeness as they were, so it never widens the range.
- **Order and paging**: sorted by `createdate` ascending, 200 results per page, following
  `paging.next.after`. `after` is omitted on a partition's first page.
- **Partitions**:
  1. `[start, end)` is cut into consecutive one-day partitions, read in ascending order.
  2. Each partition is one query with a half-open range: `createdate GTE <lo>` and
     `createdate LT <hi>`, as epoch-millisecond strings. The contract's description mentions
     `BETWEEN`, but `BETWEEN` includes both ends, so a deal on a shared boundary would be read
     twice; `GTE` and `LT` read it exactly once.
  3. When a page reports `total` above 10,000 and the partition is longer than one hour, its
     rows are discarded and it is split at a whole hour (24 → 12 → 6 → 3 → 1 + 2 → 1 + 1).
     Both halves are read in order.
  4. A partition of one hour or less is never split. When its `total` still exceeds 10,000,
     it is read up to the ceiling, and no request goes past 10,000 results. The search is
     then `incomplete` with the reason "result limit reached at one-hour partition".
  5. A partition whose paging ends with fewer distinct records than its `total` keeps its
     rows and is `incomplete` with "search paging ended before its reported total". Totals
     can race deletions, so this is an incomplete read, not a defect.
  6. A transient failure that outlasts its retries stops the search and keeps the rows
     already read. The current and every remaining partition are listed as incomplete, with
     the failure's reason, and the error is returned as `SearchOutcome.interrupted_by`.
- **De-duplication**: records are de-duplicated by `id`, keeping the first occurrence.
- **Consequence**: a search cut short by the ceiling or a short read (steps 4 and 5) is a
  completed read and marks `hubspot.deal_search` `incomplete` with its reason. The orphan-deal
  share is then unknown and deal counts carry `deal_floor`. Deals the search never returned
  cannot be counted as not assessed, so the share displays as "unknown (deal search
  incomplete)", or "unknown (deal search incomplete; at least N deals not assessed)" when N
  deals the search did return have an unknown deals-to-contacts fetch.
  An interrupted search (step 6) is a failed attempt: the deals read are stored and their
  deals-to-contacts associations still read, but no success is recorded, and the error is
  handed to `sync/mode.py`. The connection becomes `stale` at once and the failed attempt is
  recorded, while the dataset keeps its last good data (`last_success_at` and its coverage
  stand). Items reading the dataset are flagged stale once its last success is older than
  `sources.stale_after_hours` (24) or its coverage stops short of the window end
  ([P4](../recommendation-rules.md#p3-to-p8)).
- **Defects**: a page that breaks the paging contract raises a defect before another request:
  a record without a usable `id`, a cursor on an empty page, a cursor that repeats an earlier
  one, or a numeric cursor that does not advance.

One page request, with placeholders for the partition bounds:

```json
{
  "filterGroups": [
    {
      "filters": [
        {"propertyName": "createdate", "operator": "GTE", "value": "<epoch ms of partition start>"},
        {"propertyName": "createdate", "operator": "LT", "value": "<epoch ms of partition end>"}
      ]
    }
  ],
  "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
  "properties": ["createdate", "dealstage", "pipeline", "amount"],
  "limit": 200,
  "after": "<paging.next.after of the previous page>"
}
```

Bodies stay well under the 3,000-character limit; the client refuses to send one that is not.

## Optional Google-to-CRM link

The product does not assume HubSpot links a contact to a Google Ads campaign. The link exists
only when both properties are configured and confirmed:

- `HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`: a contact property holding the Google campaign id.
- `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`: a `datetime` contact property holding the instant
  that touch happened.

Startup verification confirms through the properties API that both exist on contacts and that
the touch-date property has type `datetime`. A `date` property is refused: it holds a day
without a time, which reads as midnight UTC and would let events earlier on the touch day
pass the causal check. When a check fails, the link is unavailable, with a reason such as
`HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property '<name>' not found on contacts` or
`HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY property '<name>' has type 'date', not a datetime property`.

- **Discovery** (HubSpot primary dataset only): CRM Search on `contacts` filters the
  touch-date property over the sync range, partitioned exactly like the deal search, with
  `HAS_PROPERTY` on the campaign property and results sorted by the touch date. It finds and
  stores Google-only contacts that never submitted a LinkedIn form.
- **Refresh and freshness**: the [contact refresh](#contact-refresh) then re-reads every
  stored Google-touch contact by id, whatever its touch date. `hubspot.google_contacts` is
  recorded after that refresh: `incomplete` with "N Google-touch contacts not refreshed" when
  any was not read, and covering the whole stored range, unless a stored ceiling or short-read
  discovery shortfall keeps the coverage to this discovery's range. An interrupted discovery,
  or an interrupted refresh that left a Google-touch contact unread, records a failed attempt
  instead.
- **Associations**: every stored Google-touch contact, whatever its touch date, gets the same
  contacts-to-meetings and contacts-to-deals reads as matched contacts.
- **Anchor**: the touch date anchors the causal check and lead maturity for Google
  (`crm.lead_maturity_days`, default 14). Each discovered contact counts as one matched lead
  at its touch date.
- **Dataset**: the Google-to-CRM link always reads HubSpot's active primary dataset, live or
  simulated, never a companion.
- **Configuration drift**: the contact reads include the Google properties only while the
  settings still name the properties that were verified. A change takes effect after the
  next verification, at worker restart.
- **Without both properties**: the Google pipeline link is `unavailable`. Google items carry
  the `no_crm_link` flag, and no Google QL count or Google pipeline veto is computed. A
  LinkedIn-derived subset is never used in their place.

A discovery page request:

```json
{
  "filterGroups": [
    {
      "filters": [
        {"propertyName": "<touch-date property>", "operator": "GTE", "value": "<epoch ms of partition start>"},
        {"propertyName": "<touch-date property>", "operator": "LT", "value": "<epoch ms of partition end>"},
        {"propertyName": "<campaign property>", "operator": "HAS_PROPERTY"}
      ]
    }
  ],
  "sorts": [{"propertyName": "<touch-date property>", "direction": "ASCENDING"}],
  "properties": ["email", "hs_additional_emails", "lifecyclestage", "lastmodifieddate", "<campaign property>", "<touch-date property>"],
  "limit": 200
}
```

The `properties` list also carries the bound MQL and QL date properties when they are
available.

## Errors, fallback and recovery

Connectors raise only three error kinds, and `sync/mode.py` alone decides what follows. The
platform-wide table is in [architecture.md](../architecture.md); for HubSpot:

| Condition | Classification | Behaviour |
|-----------|----------------|-----------|
| `HUBSPOT_ACCESS_TOKEN` unset or blank | Access unavailable | HubSpot runs on simulated data, reason `credentials not configured` |
| HTTP 401 or 403, including 403 `MISSING_SCOPES` | Access denied | The whole source switches to simulated data; the reason records the platform code and the failing endpoint |
| HTTP 429 (`RATE_LIMIT`), 5xx, timeout, network failure | Transient | 3 attempts with backoff, `Retry-After` honoured; afterwards the connection becomes `stale` at once and a failed attempt is recorded on each affected dataset's freshness row; the dataset keeps its last good data, and its `last_success_at`, coverage and completeness stand. A failed attempt alone flags no item: items reading the dataset are flagged stale once its last success is older than `sources.stale_after_hours` (24) or its coverage stops short of the window end ([P4](../recommendation-rules.md#p3-to-p8)). Never mock |
| HTTP 400 or another 4xx, a response that breaks its contract, a body the client refuses to send | Defect | The job fails and is logged. No retry, no mock |

- **Per input, not per call**: inside a sync, the batch methods record per input or per id a
  transient failure that outlasted its retries, a batch status other than `COMPLETE`, and
  each 207 error entry. The sync keeps what it read and marks the rest unknown (`failed` in
  a lookup, `contact_read_status = error`, a `partial` or `error` fetch, missing target
  ids). Access denied and defects always propagate. The first transient failure of a batch
  method call, or the one that stopped a partitioned search, is also returned in the result's
  `interrupted_by`, and the sync hands it to `sync/mode.py`, which fails that dataset's
  refresh: the connection becomes `stale` at once and a failed attempt is recorded, while
  `last_success_at` and the coverage stand; items reading the dataset are flagged stale once
  its last success is older than `sources.stale_after_hours` (24) or its coverage stops short
  of the window end. A batch status other than `COMPLETE` and a 207 error entry stay per-input
  outcomes only. A matching re-fetch or lookup failure (`MatchReport.interrupted_by`) is
  handed over with no dataset, its rows already `error` and due for a retry: by the CRM sync,
  the matching job and a build's own matching pass alike. `run_matching` hands each one to its
  caller as soon as it is observed, so one met before a later batch raises is handed over too,
  ahead of the raised error, whose consequence stands.
- **Stale**: an item is flagged stale when a dataset it reads last succeeded more than
  `sources.stale_after_hours` (24) ago or covers less than the window end.

**Automatic fallback and recovery**: a HubSpot source simulated after a denial, or after a
live build that failed, while `DATA_MODE=auto` and a token is set, is in automatic fallback.
The scheduler then probes it every 30 minutes (`PROBE_INTERVAL`). `HubSpotClient.probe`
runs, in order:

1. a one-email contacts batch read of `ads-engine-probe@ads-engine.invalid`, a reserved
   `.invalid` address that names no real contact (`OBJECT_NOT_FOUND` counts as success);
2. a one-result deals search with no filters, sorted by `createdate`;
3. a one-input associations read: deals to contacts on the deal found, or contacts to deals
   on id `0` when the portal has no deals.

A batch status other than `COMPLETE` fails its check.

- **All checks pass**: the worker builds a live HubSpot dataset, backfills it, stages and
  attempts the matches, and activates it only once the staged-matching readiness test passes
  ([Staged matching](#staged-matching-on-a-crm-dataset-change)).
- **Build fails**: the building dataset is discarded and the simulated one stays active.
- **Partial success**: nothing changes, so the source never flips back and forth.
- **Forced simulation**: `DATA_MODE=simulated` never probes and never promotes HubSpot to
  live, even when the probes would pass.
- **First start with a token**: adding `HUBSPOT_ACCESS_TOKEN` and restarting the worker makes
  it probe HubSpot and build a live dataset through the same readiness path, with no code
  change. Operator steps are in [runbook.md](../runbook.md).

## Simulation

With no token, after a denial, or under `DATA_MODE=simulated`, HubSpot is served by the
seeded simulated portal (`MOCK_SEED`, default `42`).

- **Same wire shapes**: `simulation/payloads.py` renders the world in the live contracts:
  `paging.next.after` and `total` on CRM Search pages, per-input association paging, 207
  `errors[]` (including the `NO_ASSOCIATIONS_FOUND` entry) and lifecycle stage options.
  Requests are validated against the same contract files, so a request missing a required
  field fails in simulated mode too.
- **Same code path**: `simulation/transport.py` serves the payloads through an httpx
  `MockTransport`, so `HubSpotClient` and the parser run unchanged.
- **LinkedIn simulated**: building a simulated LinkedIn dataset also builds a companion CRM
  dataset (`source=hubspot`, `mode=simulated`, `role=companion`, parent = the LinkedIn
  dataset).
  - The companion holds the generated contacts, meetings, deals, associations, association
    fetches and `lead_match` rows, and writes its own `hubspot.contacts`,
    `hubspot.associations` and `hubspot.deal_search` freshness rows.
  - It activates and retires in the same transaction as its parent.
  - LinkedIn-attributed evidence (C1b, the C2 veto, W1 and W2) reads the companion while
    LinkedIn is simulated, so simulated leads never join live contacts.
  - Simulated leads carry emails on the reserved `sim.invalid` domain, which a live HubSpot
    client never sends.
- **HubSpot simulated while LinkedIn is live**: the simulated transport answers batch reads
  for real lead emails with seeded contacts for a deterministic share of them
  (`live_lead_match_share` in `simulation/generator.py`), keyed by email hash.
  - Those contacts exist only in the simulated HubSpot dataset.
  - Simulated HubSpot object ids come from the reserved range 9900000000000000 to
    9900999999999999, far above live HubSpot ids, so they never collide.
  - They retire with their dataset when a live HubSpot dataset activates, and leave every
    computation at once. The live dataset's matches are staged before it activates.
- **Mixed data**: items built on joins between a live and a simulated dataset carry
  `data_status = mixed`.

## Logging and personal data

- Log events (`hubspot.verify`, `hubspot.batch_read`,
  `hubspot.batch_read.simulated_withheld`, `hubspot.associations`, `hubspot.search`,
  `hubspot.probe` and the `sync.*` events) carry object types, property names, counts and
  statuses. The HTTP layer's `http.request` and `http.response` add the method and the
  request path and query as `redact_url` masks them, and `http.response` the HTTP status
  and elapsed milliseconds; `http.retry` adds the endpoint label, the attempt number, the
  wait in seconds and the error's kind, code, HTTP status and `Retry-After`.
- A failed call is also logged with the error kind, code, HTTP status, endpoint and
  `reason`: in `connector.error`, `hubspot.batch_read.call_failed`,
  `hubspot.associations.call_failed`, `hubspot.search_failed`, a `hubspot.search` that a
  transient error cut short, and `sync.classification`. `sync.classification` is the sync
  layer's one record of the error: `on_error` in `sync/mode.py` logs it with the
  disposition (`ignored`, `switched_to_simulated`, `stale` or `defect`) and, for a sync
  job, the job's name. A job's outcome is its one `sync.job.finish`, logged at `error` level
  when the job failed (`warning` when it was cancelled, `info` when it succeeded). Its
  `disposition` is the classification of the last error the job handed to `on_error`; its
  `error` is the sanitized failure text the job's failed `sync_run` rows record (a connector
  error's `reason`), never an unexpected exception's own text; its `error_type` is the class
  name of the exception that ended the job (the connector error handed to `on_error`, or one
  the run raised), null when none did.
- `reason` holds HubSpot's error message only after `sanitize_message`: emails become
  `[REDACTED_EMAIL]`; configured secrets, `Bearer` tokens and credential assignments become
  `[REDACTED]`; the message is cut to 300 characters inside a `reason` of at most 500.
- No event carries a raw response body, an unsanitised HubSpot error message (which can
  echo the email that was looked up), an email, a token or an `Authorization` value. No
  log call passes a request body, a filter value or a cursor: search events name only the
  filter properties, and paging shows only as `more`.
- Error messages are sanitised when the error is built, so a mode reason or a `sync_run`
  error never holds an email or a secret.
- Emails are persisted only as SHA-256 hashes, in `lead.email_sha256` and
  `crm_contact.email_sha256`. Free-text form answers are not stored.

## Contract files and references

Each HubSpot surface has one contract file in `backend/tests/contracts/hubspot/`. It records
the official reference URL, the API version, the page's last-modified date, the request shape
(method, URL, headers, body schema and limits) and the documented sample. The simulator and
the contract tests both read these files, so neither encodes its own assumptions.

| Contract file | Reference page (`reference_url`) | `api_version` | `last_modified` |
|---------------|----------------------------------|---------------|-----------------|
| `objects_batch_read.json` | <https://developers.hubspot.com/docs/api-reference/latest/crm/objects/objects/batch/get-objects> | `2026-09` | 2026-09-14 |
| `crm_search.json` | <https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm> | `2026-09` | 2026-09-09 |
| `associations_batch_read.json` | <https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/batch/get-associations> | `2026-09` | 2026-09-09 |
| `properties.json` | <https://developers.hubspot.com/docs/api-reference/latest/crm/properties/get-property> | `2026-09` | 2026-09-09 |

Other HubSpot sources this page relies on:

- the "Associate records" guide (`associations/associate-records/guide` under the same
  reference root): the 1,000-input batch-read limit;
- the changelog "Introducing date-based API versioning" (2026-03-30): the version support
  policy.

**Live captures**: once the token works, `ads-engine capture-fixtures --source hubspot` records
one live request and response per surface:

```bash
cd backend
uv run --frozen ads-engine capture-fixtures --source hubspot
```

Inside the backend image the same command runs as `ads-engine capture-fixtures --source hubspot`
from `/app`. The captures then land in `/app/tests/contracts/hubspot/captured/`, which only the
runtime user (UID 10001) can write. On the host as in the image, the command is refused, with
nothing sent, when that directory is not writable by the user running it, or it or a directory on
its path below the working directory (`tests`, `tests/contracts` or `tests/contracts/hubspot`) is
a symbolic link. To keep the files, bind-mount the checkout's
`backend/tests/contracts/hubspot/captured/` there, writable by UID 10001, or run the container
with `--user` set to that directory's owner.

- It sends minimal reads: the `lifecyclestage` property with binding verification, a
  one-result deal search over the last 30 days, that deal's contacts (or one input when there
  is no deal), and that contact by id (or the probe email).
- Request counts exclude retries. `crm_search`, `associations_batch_read` and
  `objects_batch_read` cost one request each, and `objects_batch_read` is skipped when binding
  verification fails. The associations read, `associations(..., max_continuations=0)`, sends
  the initial batch call only: an input whose result carries `paging.next.after` is reported
  `partial` (`error` with no target read), with the reason "continuation limit reached; later
  pages were not read", and its continuation is never sent. Sync passes no bound and follows
  every continuation.
- `properties` costs the `lifecyclestage` read plus the fixed reads of CRM binding
  verification, which reads each property it needs once: `lifecyclestage` again when a
  binding is in `stage_label` mode, the `hs_v2_date_entered_{stage id}` property of the stage
  each such binding matches, a `property`-mode binding's own property, and both Google link
  properties when `HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY` and `HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`
  are both set. No property is listed and nothing is paged. With the shipped mapping, when
  both bindings match a stage, that is four requests; the Google link properties add two.
- It writes `backend/tests/contracts/hubspot/captured/{stem}.json`, for example
  `captured/objects_batch_read.json`, each shaped `{surface, captured_at, request, response}`.
- Emails, names, identifying contact property values, the `Authorization` credential and
  every configured secret are replaced by `"<redacted>"`; `Authorization` keeps its scheme.
  A configured secret is replaced in any string whatever its length, in plain text or in any
  percent-encoded spelling. A capture that still holds one, however many times it was
  percent-encoded, is not written, and the command exits with status 1.
- The contract tests then validate the parser against the captured payloads as well as the
  documented samples.
- The command is refused under `DATA_MODE=simulated` or without the token. It exits with
  status 1 when a capture breaks its contract, and still writes that capture, because it is
  the live truth the contract tests must flag.
