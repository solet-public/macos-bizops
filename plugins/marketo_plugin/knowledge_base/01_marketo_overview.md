# Marketo Plugin (`marketo_plugin`)

A marketing-automation connector over the operator's Marketo Engage instance.
Executor: a plain synchronous `httpx.Client` (no official Marketo SDK, no
CLI). Auth: OAuth 2.0 client-credentials against a LaunchPoint custom service
— the same auth-simplicity class as `zuora_plugin`. Single instance v1: the
"marketo_instance" address-book entry.

## Read/write posture — full CRUD, INCLUDING delete and merge

Unlike `zuora_plugin`'s typed billing-object workflow, Marketo ships native
`delete_leads` and `merge_leads` verbs. Vendor RBAC controls which operations
the registered Marketo role may perform.
Marketo's REST API supports lead deletion directly
(`POST /rest/v1/leads/delete.json`) and lead merging directly
(`POST /rest/v1/leads/{winning_id}/merge.json`), and lead records don't carry
the same "immutable financial record" semantics Zuora's billing objects do —
deleting or merging a lead is a normal, supported operation in Marketo's own
admin UI. This makes `marketo_plugin` the first marketing-data connector in
the suite with destructive verbs; call them out explicitly to the operator
rather than folding them in silently (see `plugin.yaml`'s description and the
hydration pitch below).

`merge_leads` is IRREVERSIBLE by Marketo's own design (there is no
"unmerge") and caps at 25 losing leads per call — or exactly 1 when
`merge_in_crm=true`, a restriction Marketo itself imposes on CRM-synced
merges (not an arbitrary choice made here). Marketo's own 1080 error code
(server-enforced batch-size limit, effective 2026-03-31) backstops the
client-side cap.

Field precedence has a measured read-only exception that Adobe's general
merge documentation does not disclose. Writable fields normally keep a
present winner value and may fill a null winner from the first eligible losing
record. Read-only fields instead keep the winning record's value, including an
empty one, and are never populated from a losing record. Do not rely on
Adobe's general "some value is better than no value" rule for a read-only
field; it predicts the opposite of observed Marketo behavior.

## Asset folders and My Tokens (build 4)

All ten Adobe Folder/My Token endpoints have typed async verbs. Folder reads:
`get_asset_folder` (numeric `folder_id`, `folder_type`),
`get_asset_folder_by_name` (`name`, optional `root`/`folder_type`/`workspace`),
`list_asset_folders` (optional JSON `root`, `max_depth`, `workspace`), and
`get_asset_folder_content` (`folder_id`, `folder_type`). Reads require
`output_tsv_path` under a configured export root. Browse/content accept every
vendor paging parameter as `max_return`/`offset`, page internally to 500 rows
by default (up to 5,000 with `acknowledge_default_limit_override` and
`row_limit`), and return `next_offset` when truncated. Resume with that offset
and the same filters; a partial last page is not skipped. Page metadata keeps
vendor requestId, warnings and errors. Results remain TSV handles.

`create_asset_folder` takes `name`, JSON `parent` (`id`, `type`) and optional
`description`. `update_asset_folder` takes `folder_id`/`folder_type` and optional
`name`, `description` (empty string clears), `is_archive` (true archives,
false unarchives). `delete_asset_folder` takes `folder_id`/`folder_type`;
Marketo decides whether it is an empty non-system folder. Programs returned
by reads require the separate Program API for mutations. No published folder
move, clone or approve operation is invented.

`get_asset_tokens` takes `folder_id`, optional `folder_type` (vendor default
Folder), and `output_tsv_path`. `set_asset_token` takes `folder_id`,
`folder_type`, `name` (up to 50 characters), `token_type` (date, number,
rich text, score, sfdc campaign, text), and string `value`. It creates or
corrects an existing local My Token; an empty string clears its value.
`delete_asset_token` takes the same key without `value`; recreate with
`set_asset_token` and the saved value to reverse a deletion. Token reads export
the vendor's nested folder/tokens structure. These tokens are asset content;
OAuth bearer credentials remain in memory.

Folder and token writes are form encoded; embedded references are serialized
as JSON. Poll `check_marketo_job_status` after every dispatch. Asset write
results preserve the vendor envelope, without fabricated batch tallies. They
use the registered user's Read-Only Assets / Read-Write Assets and workspace
permissions. `check_setup` performs no Asset probes and lists these writes
as unverified. No plugin confirmation or read-only gate is added.

There is no expected-version/ETag on these vendor endpoints. Concurrent writes
are last-write-wins; serialize edits and read back before retrying an uncertain
write. Job results retain each observation; they are not a vendor change log.
For exact source verification and remaining families see `vendor_api_coverage.md`.

## Budgeting a batch — `get_api_usage`

`get_api_usage` reads Marketo's current-day subscription usage summary from
`/rest/v1/stats/usage.json` and returns `calls_today` plus the per-API-user
breakdown. Use it before and during high-volume work so the local runner's own
counter is reconciled with calls made by other integrations. The endpoint
reports consumption, not the account's purchased quota limit: compare
`calls_today` against an operator-confirmed quota and retain headroom for
concurrent production integrations.

## Discovering query filters — `describe_lead_fields.searchable_fields`

`describe_lead_fields` returns both the field descriptors and the configured
instance's `searchable_fields`. Those names are the source of truth for
`get_leads.filter_type`, including eligible custom fields; the plugin forwards
the selected name and lets Marketo validate it. Do not constrain a caller to a
static cross-instance list when the describe response exposes the real
instance-specific contract.

## Lead Database objects — companies, opportunities, opportunity roles, sales persons

Each of the four objects has four typed, async verbs: `describe_<object>`,
`get_<object>`, `sync_<object>` and `delete_<object>` (`<object>` is
`companies`, `opportunities`, `opportunity_roles` or `sales_persons`). They
share one vendor contract, verified against Adobe's Lead Database reference:

| Operation | Method and path | Notes |
|---|---|---|
| describe | `GET /rest/v1/<base>/describe.json` | Field list to TSV; returns `id_field`, `dedupe_fields`, `searchable_fields`. |
| get | `GET /rest/v1/<base>.json` | `filterType`, `filterValues` (≤300), `fields`, `batchSize=300`, `nextPageToken` paged internally to the same 500/5,000 row limit as `get_leads`. |
| get (roles, compound key) | `POST /rest/v1/opportunities/roles.json?_method=GET` | `compound_keys`: each with `externalOpportunityId`, `leadId`, `role`. |
| sync | `POST /rest/v1/<base>.json` | `action` createOrUpdate/createOnly/updateOnly, `dedupe_by` dedupeFields/idField, ≤300 `records`. Companies use this collection path too, not a `/sync.json` suffix. |
| delete | `POST /rest/v1/<base>/delete.json` | `delete_by` dedupeFields/idField, ≤300 `keys`. Destructive. |

`<base>` is `companies`, `opportunities`, `opportunities/roles` or
`salespersons`. Keys: companies `externalCompanyId` / `id`; opportunities
`externalOpportunityId` / `marketoGUID`; opportunity roles the compound
`externalOpportunityId` + `leadId` + `role` / `marketoGUID`; sales persons
`externalSalesPersonId` / `id`. Reads write a workspace TSV; writes and deletes
return Marketo's per-record `result` array and tallies through
`check_marketo_job_status`. A `skipped` record (for example `1013 Record not
found`, or a sales person still linked to active leads) is data, not a job
failure. Vendor RBAC is the boundary: there is no plugin-side confirmation or
read-only gate on `sync_`/`delete_`. Adobe documents the opportunity-role API
as unavailable on subscriptions with a native CRM sync; the vendor error is
returned as-is. Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.

## Named accounts, named account lists and custom objects

Build 3 extends the same contract, verified on 2026-11-07 against Adobe's
published OpenAPI document (`swagger-mapi.json`) and the Named Accounts,
Named Account Lists and Custom Objects reference pages:

| Verbs | Method and path | Notes |
|---|---|---|
| `describe_`/`get_`/`sync_`/`delete_named_accounts` | `/rest/v1/namedaccounts` (`/describe.json`, `.json`, `/delete.json`) | Keys `name` (dedupe) / `marketoGUID`. Sync takes createOrUpdate (default), createOnly or updateOnly. |
| `get_`/`sync_`/`delete_named_account_lists` | `/rest/v1/namedAccountLists` (`.json`, `/delete.json`) | No describe endpoint, and the query takes no `fields`. Sync takes only createOnly (default) or updateOnly; `name` is the one updateable field. Lists with `type: external` are CRM-managed and `updateable: false`. |
| `get_named_account_list_members` | `GET /rest/v1/namedAccountList/{list_id}/namedAccounts.json` | `fields`, `batchSize=300` and `nextPageToken`, paged internally to the 500/5,000 row limit. |
| `add_`/`remove_named_account_list_members` | `POST /rest/v1/namedAccountList/{list_id}/namedAccounts.json` and `POST /rest/v1/namedAccountList/{list_id}/namedAccounts/remove.json` | ≤300 `named_account_ids` (marketoGUIDs). Removing a member does not delete the account. |
| `list_custom_object_types` | `GET /rest/v1/customobjects.json` | Optional `names`. Returns `api_names` and a TSV of each type's id field, dedupe fields, searchable fields and relationships. |
| `describe_`/`get_`/`sync_`/`delete_custom_objects` | `/rest/v1/customobjects/{api_name}` (`/describe.json`, `.json`, `/delete.json`) | `api_name` is the type's API name. `get_custom_objects` also takes `compound_keys`, sent as `POST ...?_method=GET`. Keys are the type's own dedupe fields / `marketoGUID`. |

Compound-key queries (`get_opportunity_roles` and `get_custom_objects`) send
`filterType` (default `dedupeFields`), `fields`, `input` and `batchSize` in the
JSON body. They page on the `nextPageToken` returned in the body to the same
row limit as every other read, so they never stop after one call. A caller's
`api_name` and `list_id` are percent-encoded into a single path segment; `.`
and `..` are refused because they would address a different endpoint.

Writes and deletes are first-class, with no plugin-side confirmation. The
vendor Role's Named Account, Named Account List and custom-object permissions
decide what the API user may do. Custom object type schema management (the
`/rest/v1/customobjects/schema...` endpoints) and the named account field
schema browse remain reachable through `request` until a later build adds
typed verbs.

## Paging contracts differ by verb — internal now, but still not one signal

**Internal pagination is hidden by design (2026-08-03 hide-paging change,
operator ruling "the paging is an implementation detail that should be
hidden," design doc §5.4/§7.2 as amended).** `get_leads`, `list_campaigns`, `list_static_lists`, and
`get_activities` all page INTERNALLY — no `next_page_token` or
`more_result` field exists on any of the four, input or output. The internal
loop runs to completion up to the effective row limit (§5:
`acknowledge_default_limit_override`/`row_limit`, default 500, hard cap
5,000) and produces one complete file with a `truncated` boolean — the
vendor's own per-call ceiling is never a caller-facing continuation. This
section documents the internal mechanism
(`marketing_actions._paginate_token_authoritative` / `_paginate_activities`)
for anyone maintaining it — it is no longer a caller-facing contract, but the
underlying vendor quirks it papers over are unchanged and still worth
knowing.

**D0.3 sync-verb migration (2026-08-09) layers a SECOND change on top of the
above, at the dispatch level, not the pagination level.** All four verbs now
dispatch an async job and return `{job_id, status}` in milliseconds; the
internal pagination described in this section runs in a background worker,
off the dispatch path, and a direct `process_call` caller retrieves the
finished file (or a `truncated`/error outcome) with a second call,
`check_marketo_job_status(job_id)` — never in the same round-trip as the
dispatch. A caller inside a flow gets the finished result routed back
automatically via `completion_handlers` instead. Do not read "one call, one
complete file" anywhere below as describing the CALLER's round-trip count —
it describes the vendor-facing internal loop only.

**Batch 2 (same 2026-08-09 migration) extends this to the plugin's three
single-vendor-call reads: `describe_lead_fields`, `list_activity_types`, and
`get_api_usage`.** None of the three ever looped internally (unaffected by
the pagination discussion above), but each still made its one vendor call
inline on the dispatch path before this batch — same fix, no pagination
loop to move, just the single call. `describe_lead_fields`' "returns...
searchable_fields" and `get_api_usage`'s "returns `calls_today`..." above
describe what the FINISHED job's result carries, retrieved via
`check_marketo_job_status(job_id)` — not what the dispatch call itself
returns, which is `{job_id, status}` only, same as the four above.

**The writes and diagnostics followed on 2026-11-08 (born-async
completion).** `create_or_update_leads`, `delete_leads`, `merge_leads`,
`trigger_campaign`, `add_leads_to_list`, `remove_leads_from_list`,
`test_connection` and `check_setup` were the last verbs that called Marketo
on the dispatch path. Each now checks its parameters at dispatch (a bad
batch, an over-cap list or a malformed token is still refused immediately,
with no job), returns `{job_id, status}`, and does every vendor call in the
same worker — including `create_or_update_leads`' read-only field preflight,
`test_connection`'s address-book read and token mint, and all six
`check_setup` probes. The finished job's result is exactly what each verb
returned before: per-record `results`, `row_count` and `tallies` for the four
batch writes, `success` and `request_id` for a merge or campaign trigger, and
the diagnostic report for the two setup verbs. A vendor fault (permission,
auth, validation) or a read-only refusal now arrives in the job's `error`
field rather than in the dispatch response. No Marketo verb calls the vendor
on the dispatch path any more.

The four internally-paged reads do not share one safe raw continuation
signal:

- `get_leads`, `list_campaigns`, `list_static_lists`: a non-empty
  `nextPageToken` is authoritative. The internal loop keeps calling because
  Marketo can return a usable token with raw `moreResult: false`.
- `get_activities`: `moreResult` is authoritative. Its `nextPageToken` is a
  resumable bookmark that can remain populated on the last page, so token
  presence alone would make the internal loop never terminate.

This boundary is deliberate. A generic rule such as "always trust
`moreResult`" would truncate lead/campaign/list enumeration internally, while
"always trust the token" would loop the activity fetch forever.

**The asymmetry is OUR design decision, and the two sides do not rest on the
same evidence.** The token-authoritative side is backed by a live measurement:
`moreResult` was observed reporting false on a full 300-record
`list_campaigns` page that still returned a usable token. The
activity side rests on Adobe's documented "this endpoint always returns
`nextPageToken`" plus one narrow live read, so calling `moreResult`
"authoritative" there states what the internal loop keys off, not a verified
property of the vendor. Its reliability on `get_activities` is
**UNVERIFIED** — see the evidence classes in the traps below, and note that
before this build an under-reporting flag was survivable (the caller still
held a token and could keep going); now it means `get_activities` returns
`truncated: false` on an incomplete read with nothing for a caller to notice
with.

## Verifying what a write actually DID — `get_activities`

`merge_leads`, `delete_leads` and `create_or_update_leads` can fire smart
campaign triggers: Adobe's own documentation confirms a merge raises
lead-created / data-value-changed events, and a subscription with active
trigger campaigns can therefore send real email to real people as a
side effect of a data-cleanup operation.

`get_activities` is the read that lets a caller answer "did that write notify
anybody?" — it reads the Marketo activity log (emails sent/delivered, alerts,
sales emails, interesting moments, campaign requests, data value changes)
from an ISO-8601 instant (`since_datetime`, which mints the internal loop's
starting token — required on every call, there is no caller-supplied
continuation-token path since the hide-paging change). `activity_type_ids` is mandatory
and accepts 1-10 ids; `lead_ids` is optional with a maximum of 30. Both caps
are Marketo's own and are enforced in-plugin rather than left to a
server-side error.

**What this verb does NOT do — state this plainly to an operator who asks for
assurance.** It is an AFTER-THE-FACT audit. It reports what already happened;
it cannot promise a future merge will stay silent, because that depends on
which trigger campaigns are active and how their filters match. The defensible
workflow is: run one merge on a single sacrificial pair, then read
`get_activities` for that lead since just before the write, and inspect what
appeared. A clean activity log for one pair is evidence about that pair, not a
guarantee about the remaining batch.

Three traps worth knowing:

- **`moreResult` is the internal loop's authoritative continuation signal —
  and its unreliability now has a sharper consequence than before.**
  `moreResult: true` means the internal loop KEEPS PAGING, even when a page's
  `result` is empty. Marketo streams activities in ~300-item pages and an
  empty page mid-stream is normal. Before the hide-paging change, an under-reporting flag
  was survivable: the caller still held `next_page_token` and could keep
  going manually. Now that the token is hidden, an under-reporting flag means
  `get_activities` returns `truncated: false` on an incomplete read, with
  nothing for a caller to notice with — never report "nothing happened" from
  a result whose `truncated` came back false without independently trusting
  that flag's reliability.

  **Name the evidence class before repeating the word "authoritative."** Three
  distinct claims sit behind it, and only two are observations:

  | Claim | Evidence class |
  |---|---|
  | The flag does not under-report at END OF STREAM | **Measured** — 2026-07-30, one live instance, ONE one-hour window paged to termination twice under two type filters 21 seconds apart. That is one sample measured twice, not two independent runs; the near-identical row totals follow from the shared window rather than replicating each other. Both terminations landed on a SHORT page, and a probe issued past the terminal token returned nothing. |
  | The flag does not under-report MID-STREAM | **No observation at all.** The truncating mode is the flag going false on a FULL page with another page behind the returned token — the shape actually seen on `list_campaigns`. It never occurred in that read, so it is UNEXERCISED, not refuted. |
  | A page can carry fewer than 300 items while the flag is still true | **Documented** by Adobe, unobserved in that read (no short page appeared while `moreResult` was true). Ten pages is not a sample and does not refute the vendor's statement — the internal loop keeps tolerating a short mid-stream page rather than stopping early. |

  So the defensible summary is: no evidence the flag lies at end of stream, on
  one hour of one instance's traffic, and nothing at all about the mid-stream
  case, under load, or across a campaign send. It remains UNVERIFIED that
  `moreResult` is reliable on activities. Do not let the measurement retire
  the hedge — there is no fallback signal here, so a lying flag truncates the
  read silently, and it now does so behind a `truncated: false` a caller has
  no way to independently check.
- **The activity token is a resumable bookmark internally, not proof of
  another page — and it never reaches the caller.** Marketo can return
  `nextPageToken` on the final page too. The internal loop stops when
  `moreResult` becomes false even if the token remains populated; if
  `moreResult` claims true with no token to continue on (a
  documented-impossible but unmeasured-reliability edge), the loop stops
  rather than spins and reports `truncated: true`.
- **Activity type ids are not guaranteed identical across subscriptions.**
  Call `list_activity_types` on the configured instance and pass only ids it
  returns. The vendor behavior that makes this load-bearing was measured
  against a live instance on 2026-07-29: **one invalid id rejects the ENTIRE
  request**, not just the offending id, so a single stale id taken from
  another subscription's catalog fails the whole read. `constants.py` retains
  a starting-point id table and no verb applies it as a silent default. Which
  ids a given subscription accepts is a property of that instance, so it is
  not recorded here.

## Enumerating campaigns and lists — completeness is `truncated`, not optional paging

`list_campaigns` and `list_static_lists` page internally across Marketo's
300-record vendor ceiling up to the effective row limit (500 default, 5,000
hard cap via the §5 override) — a caller gets one call and one file. The
internal loop keys off `nextPageToken` presence, not Marketo's raw
`moreResult` flag, because the flag can say false while another page exists
(the one live violation of `moreResult` anywhere was observed here, on
`list_campaigns`, exactly this shape). **If `truncated` is true the result is
an arbitrary slice, not the full set** — an instance holding tens of
thousands of campaigns will otherwise hand back only the effective-limit
count, and without pushing `row_limit` up (or narrowing the filter), repeat
calls return the SAME truncated slice, which reads as data rather than as
truncation. Any question of the form "which active trigger campaigns could
this write fire?" requires confirming `truncated` is false first, raising
`row_limit` or narrowing `names`/`program_names` if it is not.

## Setup verification — `check_setup`, and the Role/User/Service prerequisite

Marketo REST access needs three admin-console objects that don't exist by
default: an API-only **Role** (specific "Access API" permission checkboxes),
an API-only **User** assigned that Role, and a **LaunchPoint Custom Service**
bound to that user (which is what actually mints client_id/client_secret).
Many instances already have this from a prior integration — check
Admin → LaunchPoint for a reusable service before building a new one. Full
operator walkthrough (which permissions, exact console paths, the standard
agent-blind secret-ingestion procedure): `knowledge_base/hydration_guidance.md`.

Because "does the Role have the right permissions" can't be answered just by
minting a token (`test_connection` only proves the credentials are valid,
not what they can do), this plugin ships a second diagnostic verb,
`check_setup`, that runs six safe read probes
(`describe_lead_fields`, `get_leads`, `list_activity_types`, `get_api_usage`,
`list_campaigns`, `list_static_lists`)
and reports, per probe, either `ok` or the exact missing Access API
permission plus which admin screen fixes it. It is **side-effect-free by
construction** — it never calls a write/execute verb, so
it cannot itself trigger a campaign or mutate a lead just to test a
permission. Its `reads_verified` field is therefore a **partial** guarantee:
write/execute permissions (`create_or_update_leads`, `delete_leads`,
`merge_leads`, `add_leads_to_list`, `remove_leads_from_list`,
`trigger_campaign`, every Lead Database object `sync_`/`delete_` verb, and
`add_`/`remove_named_account_list_members`) are listed in `writes_unverified`
and can only be
confirmed by first real use — a missing one surfaces as
`marketo.permission_denied` naming the gap.

Both diagnostics are queued like every other verb: `test_connection` and
`check_setup` return `{job_id, status}` at once, and their report (or an
`auth_failed`/`not_configured` error) is read with
`check_marketo_job_status(job_id)`. A queued handle proves only that the job
was accepted, not that the credentials or Role work.

**`reads_verified` is partial in a second, less obvious way: whole permission
classes are out of its reach.** The Lead Database object verbs (companies,
opportunities, opportunity roles, sales persons, named accounts, named account
lists, custom objects) need the Role's Company, Opportunity, Sales Person,
Named Account, Named Account List and custom-object permissions, which no
probe exercises. Marketo
also gates the asset surface (`/rest/asset/v1/…` — programs, smart campaigns,
smart lists, emails, templates, landing pages, forms, files, tokens, folders,
tags, channels) behind the separate **Read-Only Assets** and **Read-Write
Assets** permissions, scoped by workspace. That surface is reachable today
through the generic `request` verb, writes and deletes included; all six
probes are Lead-API reads, so an instance can return `reads_verified: true`
while holding no Assets permission at all. State it as **a permission a
consumer verifies on Admin → Users & Roles before depending on it, never as an
assurance from us**; a missing one surfaces as the vendor's `603 Access denied`
(`marketo.permission_denied` on a typed verb, the raw envelope in a `request`
job result) at first real use.

## Business-data limits + data-export migration (2026-08-02)

`describe_lead_fields`, `get_leads`, `list_activity_types`, `get_activities`,
`list_campaigns`, and `list_static_lists` ALWAYS write their result to the
caller's `output_tsv_path` — never records inline, at any size
(`workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md`,
§7.1; the former blob-export/`INLINE_BYTE_CAP` branch is deleted, not
lowered). Blob storage has retired from this plugin entirely — `get_api_usage`
is the only read verb untouched by this migration (small, bounded, no PII,
outside the six §7.1 names). Neither destination is platform blob storage —
the path must be absolute, end in `.tsv`, and lie under a
saved path address, the session launch folder, or the solet home. The shared file-location guard excludes the installation tree.

**Superseded by the hide-paging change (2026-08-03) for four of the six — see below.** At
Tier-2 landing, none of the six carried an `acknowledge_default_limit_override`/
`row_limit` pair, because Marketo fixes every one of these verbs' per-call
reads at (or below) 300 records server-side with no query-side size
parameter to raise. That reasoning held only for the PER-CALL ceiling; it
never addressed a CUMULATIVE multi-call fetch, because at the time nothing
looped past one call. `describe_lead_fields` and `list_activity_types` are
still exactly this shape (single, unpaginated call, nothing to raise, no
override pair) — they are genuinely out of scope for the operator's
mass-exposure floor's row-count mechanics.

**`get_leads`, `list_campaigns`, `list_static_lists`, and `get_activities`
now page INTERNALLY — operator ruling, 2026-08-03, verbatim: "we need to
deliver the results - the paging is an implementation detail that should be
hidden" (design doc §5.4/§7.2 as amended, ruled doc-wide by
Coordinator-Day).** This reverses the wave's original Pattern B (`get_leads`
as the approved N>>500 route via caller-driven `next_page_token` looping).
Each of the four now carries the standard §5 override pair for the first
time — default 500, hard cap 5,000 (`MARKETO_LIST_ROW_LIMIT_CAP`, matching
zuora's `LIST_ROW_LIMIT_CAP` precedent) — governing the cumulative fetch
across as many internal 300-record vendor calls as it takes
(`marketing_actions._paginate_token_authoritative` /
`_paginate_activities`). No `next_page_token`/`more_result` field survives
on any of the four, input or output; a `truncated` boolean replaces them.
Beyond the hard cap: **no resumption, by design** — a caller re-invokes the
verb with a narrower `filter_values` slice (`get_leads`), a narrower
`names`/`program_names` filter (`list_campaigns`/`list_static_lists`), or a
later `since_datetime` (`get_activities`). For a caller working a lead count
well past the 5,000 hard cap, this means several separate `get_leads` calls against non-overlapping
filters (~17 internal vendor calls each to reach the 5,000 cap), not one
call plus caller-side paging — real latency and API-quota cost, disclosed in
the process description with a `get_api_usage` check suggested first. The
300/call VENDOR ceiling itself is unchanged and un-raisable; what changed is
that the caller no longer loops to reach the 500/5,000-row policy ceiling.

New error code: `marketo.export_path_refused` (path not absolute / not
`.tsv` / not contained under any saved location).

**`check_setup`'s five migrated probes do NOT touch the operator's real
export workspace, and force `row_limit=1` on the three now-internally-paged
probes.** A permission probe must succeed whether or not
a project location has been saved — `_run_read_probe` writes each
probed verb's result to a throwaway tempfile via a passthrough gate, never
the plugin's real `_export_path_gate`, and deletes the tempfile
unconditionally afterward. `get_leads`/`list_campaigns`/`list_static_lists`'
probes additionally pass `acknowledge_default_limit_override=true` +
`row_limit=1`, a quota-safety fix from the hide-paging change — without it, a cheap permission
probe on an instance with more than 300 campaigns/lists would now make a
second internal vendor call to reach the new default, which a setup check
has no business doing.

## Error model — envelope-first, not HTTP-status-first (the key divergence from zuora_plugin)

Zuora's REST API returns real HTTP status codes (401/404/429/5xx) for almost
every fault class. **Marketo returns HTTP 200 with a JSON envelope**
(`{"success": false, "errors": [{"code": "...", "message": "..."}]}`) for
almost every API-level fault instead — only the identity/token endpoint and
true transport faults (5xx, connection errors, non-JSON bodies) carry a
meaningful HTTP status. `http_client.py::MarketoClient` typed JSON helpers decode response bodies and classify on the envelope's `success`/`errors[].code`, not on `response.status_code`; the generic `request` verb instead preserves HTTP status and vendor body/errors as data, as received. See `errors.py` for the sourced
code -> (our error code, retryable) table (601/602/603/604/606/607/609/610/
612/613/615/701/702/709/714/1001-1037), built from the official Adobe Marketo
Engage REST API error-codes reference (2026-07-28).

**Three distinct auth-adjacent codes, three different fixes.** 601/602
(invalid/expired token) → `marketo.auth_failed` — the client_id/client_secret
themselves are the problem. 603 (access denied) → `marketo.permission_denied`
— the API user's Role is missing an Access API permission checkbox (Users &
Roles fix). 1008 (no access to partition) → `marketo.partition_access_denied`
— the API user isn't assigned to the right workspace/partition (a different
admin screen entirely — Users & Roles > Users, not Roles). Collapsing these
into one generic "auth failed" bucket (the initial v1 design) sends the
operator to the wrong screen for 603/1008, which is why they're split.
A gateway-level HTTP 401 or 403 that carries no vendor `errors[]` keeps its
status: queued typed verbs report it as `marketo.auth_failed` (401) or
`marketo.permission_denied` (403) rather than a generic transport fault.

**Partial-batch results are NOT errors.** `create_or_update_leads`,
`delete_leads`, `add_leads_to_list`, and `remove_leads_from_list` can return
`success: true` at the top level with individual `result[]` entries carrying
`status: "skipped"` or `"failed"` plus a `reasons` array (e.g. one lead in a
300-record batch already exists under `createOnly`). The plugin passes that
per-record array through unchanged, plus a computed `tallies` dict — this is
normal batch-operation data, not a plugin-level fault. Only a top-level
`success: false` envelope (a structural fault — bad batch shape, auth,
access) raises and gets classified.

Before `create_or_update_leads` writes, it reads this instance's lead-field
metadata once and refuses the whole batch when an intended field is marked
REST read-only. Read-modify-write is the deliberate exception: Adobe
documents that an omitted Get Leads `fields` parameter returns exactly
`id`, `email`, `updatedAt`, `createdAt`, `firstName`, and `lastName`, so the
plugin treats the intersection of that explicit default set and this
instance's live `readOnly` metadata as echoed read output rather than an
intended write. The six-field set has **documented, not measured**
provenance and does not assert which members Marketo marks read-only; the
describe response supplies that separate property at execution time.

A live describe on 2026-07-30 settled three things about that mechanism. The
preflight must key off each descriptor's **`rest.readOnly`**: the fields whose
read-only status actually blocks a read-modify-write were observed carrying a
`rest` block and **no `soap` sub-object at all**, so a preflight falling back
to `soap` would find nothing exactly where the answer matters. The intersection
was **non-empty** on that instance, so the exclusion is load-bearing rather
than a no-op — without it every read-modify-write there would have refused.
And **`id` came back `readOnly: false`, i.e. writable**, which retires the
worry that using the bare `id` as a lookup key could brick a write on a
read-only field. Which fields are read-only remains a property of the instance,
computed as a runtime intersection against its live describe; it is deliberately
**not encoded** as a list here or in `constants.py`.

This exception has an accepted silent-drop residue. Default echoes are
excluded only from the refusal; they remain in the outbound record. If a
caller genuinely means to write one that the instance marks read-only,
Marketo may silently ignore it while returning `status: "updated"` and no
`reasons[]`. Treat those fields as vendor-controlled, strip unchanged
default echoes from deliberate write payloads where practical, and verify
the writable fields that matter instead of treating an updated tally as
proof that every submitted field applied.

### Per-record rejection evidence — inspect `results`, not only `tallies`

Live-instance trials on 2026-07-30 exposed two materially different field
failure modes in Marketo's own Sync Lead response:

| Submitted record | Vendor result | Observed mutation |
|---|---|---|
| Read-only field plus writable field(s) | `status: "updated"`; no `reasons[]` entry for the read-only field | Writable fields applied and `updatedAt` moved; the read-only field was silently dropped. |
| One unknown field name plus one writable field | `status: "skipped"` with `reasons: [{"code": "1006", "message": "Field '<name>' not found"}]` | The entire record was discarded: the valid field did not apply and `updatedAt` did not move. |

The asymmetry is the trap: Marketo uses `reasons[]` for an unknown field but
leaves it silent for a read-only one. The plugin preserves each vendor result
verbatim and computes `tallies` only as a status-count summary; it does not
normalize or synthesize reasons. A caller must therefore inspect every
per-record `status` and its `reasons[]`. When one record is `skipped` for code
1006, treat every field in that record as unapplied, correct the field name,
and resend the record. A tally such as `{"updated": 9, "skipped": 1}` cannot
identify the skipped record or explain why it was discarded.

## Session model — re-mintable bearer, in-memory only, envelope-triggered re-mint

The OAuth `client_secret` is the durable credential (chain-consumed through
the address book, never vaulted under this plugin's own identity). The
bearer token it mints is short-lived (~3600s), re-mintable, and held ONLY in
process memory (`http_client.py::MarketoClient`) — never persisted. Unlike
Zuora's 401-triggers-retry (a real HTTP status), Marketo's expired/invalid
token surfaces as a `success: false` envelope with error code `601` or `602`
at HTTP 200 — `MarketoClient._request_json` inspects the decoded envelope's
first error code and triggers exactly ONE re-mint-and-retry for those two
codes specifically; any other code (including a *second* 601/602) classifies
normally.

## Registering the Role, API-only User, and LaunchPoint service (operator console runbook)

Unlike Jira (one API token) or Zuora (one OAuth client on an existing user),
Marketo REST access needs three admin-console objects built in order — and
**many instances already have all three** from a prior integration (Bizible,
a Salesforce sync, another marketing-ops tool). Check Admin → LaunchPoint for
a reusable Custom Service FIRST; only build fresh if none exists or the
existing one's Role turns out to be missing a permission `check_setup` names.

1. **Create the Role** (Admin → Users & Roles → Roles → New Role). Under the
   **Access API** permission tree, check:
   - `Read-Write Person` — covers lead read/write AND static-list membership
     add/remove (one permission gates both, per Marketo's own docs).
   - Activity permissions needed for the activity type catalog and activity
     log reads; grant the broader vendor permission if the work requires more.
   - Campaign permissions needed for listing, and `Read-Write Campaign` if
     campaign edits are wanted later.
   - `Execute Campaign` (required for `trigger_campaign` to actually fire).

   `list_static_lists` (plain enumeration, not membership writes) may need an
   additional permission whose exact name isn't published by Adobe — if
   `check_setup` flags it, its guidance points at the Access API tree
   generally rather than naming a checkbox, since a wrong guess sends the
   operator to add the wrong one.
2. **Create the API-only User** (Admin → Users & Roles → Users → Invite New
   User): check **API Only**, assign the Role from step 1.
3. **Create the LaunchPoint Custom Service** (Admin → LaunchPoint → New → New
   Service): type **Custom**, select the API-only user from step 2, Create.
4. **Get the credentials**: open the service → View Details → Get Token.
   Marketo shows Client Id, Client Secret, Authorized User, Token. The
   operator's only act here is Copy (the client secret) — everything else is
   non-secret and can be read/pasted directly.
5. **Find `base_url`** at Admin → Web Services — the REST endpoint (e.g.
   `https://123-ABC-456.mktorest.com`). The identity/token endpoint lives
   under the same host, so one field covers both (no separate identity_url,
   unlike G-Suite's split auth/API hosts).
6. **Harvest + seed the secret agent-blind**: the moment the operator says
   "copied," `pbpaste` straight into a temp file (never displayed) →
   `vault_service::store_from_file` into
   `<solet>.default_address_book_plugin.marketo_client_secret` → delete
   the temp file → clear the clipboard. Never a bespoke seed script.
7. **Register the address-book entry** `marketo_instance`: literals
   `base_url` and `client_id`, plus `client_secret` = the `vault::` reference
   from step 6.
8. Run `test_connection` then `check_setup` (see above), reading each job's
   finished report with `check_marketo_job_status`, to confirm the
   credentials work AND the Role grants what this plugin's read verbs need.

Full operator-facing phrasing (the pitch, the "check for an existing service
first" framing, round-trip count, why-secure paragraph) lives in
`knowledge_base/hydration_guidance.md` — this section is the mechanism
detail.

## Security posture (mirrors the Zuora/Salesforce/Jira/Snowflake/external-Postgres wave)

- **Foreign-target invariant.** No `base_url`/instance parameter on any verb —
  every request is built from the single registered `marketo_instance` entry.
- **Not denied from the MCP surface.** Call `process_call` on these verbs
  directly, per the 2026-07-15 operator ruling retiring the RATIFY-3
  process_export deny (friction, not security, on a single-user substrate).
- **Generic error messages for topology-leaking classes.** Auth and
  rate-limit/quota errors surface a fixed generic message, never the raw
  response body or request URL. Validation/not-found/query-failed classes
  build their message from Marketo's own `errors[].message` text — that
  describes the caller's own request, not our instance host.
- **Secrets hygiene.** The OAuth client_secret is chain-consumed through the
  address book's `resolve_with_secrets` — this plugin declares no vault keys
  of its own (`get_required_vault_keys`/`get_declared_vault_keys` both
  return `[]`). The re-minted bearer token is never vaulted.

## Files and bulk transfers (build 5)

Files/images now have typed by-id/by-name/browse, multipart create/upsert and
content replacement. Bulk Import has typed submission/status/failures/warnings
for leads, custom objects and program members. Bulk Extract has typed
list/create/enqueue/status/download/cancel for leads, activities and program
members. Every operation uses the existing deferred job worker. A platform
job finishing only confirms that HTTP operation; retain the vendor batchId or
exportId and poll its typed status verb for the vendor terminal outcome.
See `03_bulk_and_files.md` for every parameter, lifecycle and download contract.
The generic `request` also accepts multipart files with form fields.

## SQL-lockdown gate note

This connector composes NO SQL-shaped strings anywhere — every request body
is a caller-supplied JSON object, never a literal SQL/SOQL/ZOQL-shaped
string, and there is no database driver import. The SQL-lockdown gate is
silent for this plugin — no allowlist entry needed anywhere in `src/` or
`tests/`.

## Key files

| File | Purpose |
|---|---|
| `src/marketo_plugin/constants.py` | Every magic value: address-book field names, the Marketo REST error-code map, caps, error codes. |
| `src/marketo_plugin/app_config.py` | Resolves `marketo_instance` from the address book; the client_secret is chain-consumed. |
| `src/marketo_plugin/http_client.py` | `MarketoClient` — synchronous httpx client with cached, envelope-triggered re-mintable bearer auth (client-credentials grant, GET-based token mint). |
| `src/marketo_plugin/errors.py` | Envelope-first error classification (`classify_marketo_envelope`) from the response body's `errors[]` list. |
| `ananta/src/ananta/file_locations/containment.py` | Shared `assert_path_allowed` guard using saved path addresses, the session folder and solet home — admits `output_tsv_path` for the six data-export-migrated read verbs. |
| `src/marketo_plugin/marketing_actions.py` | Pure verb implementations: `describe_lead_fields`, `get_leads`, `get_api_usage`, `list_activity_types`, `get_activities`, `create_or_update_leads`, `delete_leads`, `merge_leads`, `list_campaigns`, `trigger_campaign`, `list_static_lists`, `add_leads_to_list`, `remove_leads_from_list`, `check_setup` — the six-verb data-export set write to `output_tsv_path` under the §7.1 migration (see above). Each queued verb splits into a dispatch-path `prepare_<verb>` (validation only) and a worker-side `execute_<verb>` (the vendor calls); the combined `<verb>` function remains for direct in-process callers. |
| `plugins/marketo_plugin/src/marketo_plugin/lead_database_objects.py` | Typed company, opportunity, opportunity-role and sales-person verbs: verified vendor paths, validation, pagination, per-record write results. |
| `plugins/marketo_plugin/src/marketo_plugin/lead_database_process_metadata.py` | Shared descriptions and parameter maps for those sixteen process declarations. |
| `src/marketo_plugin/plugin.py` | The `MarketoPlugin` EDGE provider — client lifecycle, error mapping, EDGE registration, `_export_path_gate`. |
