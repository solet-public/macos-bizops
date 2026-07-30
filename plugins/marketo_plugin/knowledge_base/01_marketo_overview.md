# Marketo Plugin (`marketo_plugin`)

A marketing-automation connector over the operator's Marketo Engage instance.
Executor: a plain synchronous `httpx.Client` (no official Marketo SDK, no
CLI). Auth: OAuth 2.0 client-credentials against a LaunchPoint custom service
— the same auth-simplicity class as `zuora_plugin`. Single instance v1: the
"marketo_instance" address-book entry.

## Read/write posture — full CRUD, INCLUDING delete and merge

Unlike `zuora_plugin` (create/update, no delete — billing records are voided
through Zuora's own workflow) or `snowflake_plugin`/`external_postgres_plugin`
(read-only hard), Marketo ships native `delete_leads` and `merge_leads` verbs.
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

## Paging contracts differ by verb — do not generalize

The four paged reads do not share one safe raw continuation signal:

- `get_leads`: a non-empty `next_page_token` is authoritative. The plugin
  normalizes `more_result` from token presence because Marketo can return a
  usable token with raw `moreResult: false`.
- `list_campaigns`: a non-empty `next_page_token` is authoritative for the
  same reason. Keep paging until no token is returned.
- `list_static_lists`: a non-empty `next_page_token` is authoritative. Keep
  paging until no token is returned.
- `get_activities`: `more_result` is authoritative. Its
  `next_page_token` is a resumable bookmark that can remain populated on the
  last page, so token presence alone would make paging never terminate.

This boundary is deliberate. A generic rule such as "always trust
`more_result`" truncates lead/campaign/list enumeration, while "always trust
the token" loops forever on the activity stream.

## Verifying what a write actually DID — `get_activities`

`merge_leads`, `delete_leads` and `create_or_update_leads` can fire smart
campaign triggers: Adobe's own documentation confirms a merge raises
lead-created / data-value-changed events, and a subscription with active
trigger campaigns can therefore send real email to real people as a
side effect of a data-cleanup operation.

`get_activities` is the read that lets a caller answer "did that write notify
anybody?" — it reads the Marketo activity log (emails sent/delivered, alerts,
sales emails, interesting moments, campaign requests, data value changes)
either from an ISO-8601 instant (`since_datetime`, which mints a paging token)
or by continuing a prior page (`next_page_token`). `activity_type_ids` is
mandatory on every page and accepts 1-10 ids; `lead_ids` is optional with a
maximum of 30. Both caps are Marketo's own and are enforced in-plugin rather
than left to a server-side error.

**What this verb does NOT do — state this plainly to an operator who asks for
assurance.** It is an AFTER-THE-FACT audit. It reports what already happened;
it cannot promise a future merge will stay silent, because that depends on
which trigger campaigns are active and how their filters match. The defensible
workflow is: run one merge on a single sacrificial pair, then read
`get_activities` for that lead since just before the write, and inspect what
appeared. A clean activity log for one pair is evidence about that pair, not a
guarantee about the remaining batch.

Three traps worth knowing:

- **`more_result` is the authoritative activity continuation signal.**
  `more_result: true` means KEEP PAGING, even when `records` is empty. Marketo
  streams activities in ~300-item pages and an empty page mid-stream is normal.
  Never report "nothing happened" from a partial read — continue until
  `more_result` is false.
- **The activity token is a resumable bookmark, not proof of another page.**
  Marketo can return `next_page_token` on the final page too. Stop when
  `more_result` becomes false even if the token remains populated.
- **Activity type ids are not guaranteed identical across subscriptions.**
  Call `list_activity_types` on the configured instance and pass only ids it
  returns. Dax proved why this matters on 2026-07-29: ids
  6/7/38/39/42/44/47 were accepted while id 46 was invalid, and Marketo
  rejected the entire request because of that one bad id. `constants.py`
  retains the accepted notification ids only as a starting point and no verb
  applies them as a silent default.

## Enumerating campaigns and lists — paging is not optional

`list_campaigns` and `list_static_lists` return one Marketo page (300 records)
per call. Both surface `next_page_token` and normalize `more_result` from
whether that token is non-empty; the token is authoritative because Marketo's
raw flag can say false while another page exists. **If `more_result` is true
the result is an arbitrary slice, not the full set** — an instance with 19,919
campaigns will otherwise hand back 300 of them, and repeat calls can return
DIFFERENT 300s, which reads as data rather than as truncation. Any question of
the form "which active trigger campaigns could this write fire?" requires
draining the token chain first.

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
`check_setup`, that runs six safe read-only probes
(`describe_lead_fields`, `get_leads`, `list_activity_types`, `get_api_usage`,
`list_campaigns`, `list_static_lists`)
and reports, per probe, either `ok` or the exact missing Access API
permission plus which admin screen fixes it. It is **read-only and
side-effect-free by construction** — it never calls a write/execute verb, so
it cannot itself trigger a campaign or mutate a lead just to test a
permission. Its `reads_verified` field is therefore a **partial** guarantee:
write/execute permissions (`create_or_update_leads`, `delete_leads`,
`merge_leads`, `add_leads_to_list`, `remove_leads_from_list`,
`trigger_campaign`) are listed in `writes_unverified` and can only be
confirmed by first real use — a missing one surfaces as
`marketo.permission_denied` naming the gap.

## Error model — envelope-first, not HTTP-status-first (the key divergence from zuora_plugin)

Zuora's REST API returns real HTTP status codes (401/404/429/5xx) for almost
every fault class. **Marketo returns HTTP 200 with a JSON envelope**
(`{"success": false, "errors": [{"code": "...", "message": "..."}]}`) for
almost every API-level fault instead — only the identity/token endpoint and
true transport faults (5xx, connection errors, non-JSON bodies) carry a
meaningful HTTP status. `http_client.py::MarketoClient` decodes every
response body and classifies on the envelope's `success`/`errors[].code`,
not on `response.status_code`. See `errors.py` for the sourced
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
   - `Read-Only Activity` — covers the activity type catalog and activity log
     reads.
   - `Read-Only Campaign` (campaign listing; use `Read-Write Campaign` instead
     if campaign edits are wanted later — this plugin never writes one).
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
   `<homunculus>.default_address_book_plugin.marketo_client_secret` → delete
   the temp file → clear the clipboard. Never a bespoke seed script.
7. **Register the address-book entry** `marketo_instance`: literals
   `base_url` and `client_id`, plus `client_secret` = the `vault::` reference
   from step 6.
8. Run `test_connection` then `check_setup` (see above) to confirm the
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

## Scope note — no async Bulk Extract job flow (v1)

Marketo's Bulk Extract API (create job → enqueue → poll status → download
file) is a materially different, multi-call control-flow shape from every
other verb in this connector and is explicitly deferred out of v1.
`get_leads` covers ad-hoc reads with the same inline-or-spill envelope the
other connectors use (`result_blob_key` + `row_count` when the result exceeds
the inline byte cap), which covers the common case without the async job
machinery.

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
| `src/marketo_plugin/marketing_actions.py` | Pure verb implementations: `describe_lead_fields`, `get_leads`, `get_api_usage`, `list_activity_types`, `get_activities`, `create_or_update_leads`, `delete_leads`, `merge_leads`, `list_campaigns`, `trigger_campaign`, `list_static_lists`, `add_leads_to_list`, `remove_leads_from_list`, `check_setup`. |
| `src/marketo_plugin/plugin.py` | The `MarketoPlugin` EDGE provider — client lifecycle, error mapping, EDGE registration. |
