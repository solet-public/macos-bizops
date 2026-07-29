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
or by continuing a prior page (`next_page_token`). `lead_ids` (max 30) and
`activity_type_ids` (max 10) filter server-side; both caps are Marketo's own
and are enforced in-plugin rather than left to a server-side error.

**What this verb does NOT do — state this plainly to an operator who asks for
assurance.** It is an AFTER-THE-FACT audit. It reports what already happened;
it cannot promise a future merge will stay silent, because that depends on
which trigger campaigns are active and how their filters match. The defensible
workflow is: run one merge on a single sacrificial pair, then read
`get_activities` for that lead since just before the write, and inspect what
appeared. A clean activity log for one pair is evidence about that pair, not a
guarantee about the remaining batch.

Two traps worth knowing:

- **`more_result: true` means KEEP PAGING, even when `records` is empty.**
  Marketo streams activities in ~300-item pages and an empty page mid-stream is
  normal. Never report "nothing happened" from a partial read — drain the token
  chain until `more_result` is false.
- **Activity type ids are not guaranteed identical across subscriptions.** The
  ids commonly cited for the notifying activities (6 Send Email, 7 Email
  Delivered, 38 Send Alert, 39 Send Sales Email, 46 Interesting Moment, 47
  Request Campaign, 42/44 SFDC campaign add/status change) are recorded in
  `constants.py` as a STARTING POINT and are explicitly marked unverified — no
  verb applies them as a silent default. For an answer that has to be
  defensible, read `GET /rest/v1/activities/types.json` on the actual instance
  and use its ids.

## Enumerating campaigns and lists — paging is not optional

`list_campaigns` and `list_static_lists` return one Marketo page (300 records)
per call. Both now surface `next_page_token` and `more_result` verbatim from
the response. **If `more_result` is true the result is an arbitrary slice, not
the full set** — an instance with 19,919 campaigns will otherwise hand back 300
of them, and repeat calls can return DIFFERENT 300s, which reads as data rather
than as truncation. Any question of the form "which active trigger campaigns
could this write fire?" requires draining the token chain first.

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
`check_setup`, that runs four safe read-only probes
(`describe_lead_fields`, `get_leads`, `list_campaigns`, `list_static_lists`)
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
| `src/marketo_plugin/marketing_actions.py` | Pure verb implementations: `describe_lead_fields`, `get_leads`, `get_activities`, `create_or_update_leads`, `delete_leads`, `merge_leads`, `list_campaigns`, `trigger_campaign`, `list_static_lists`, `add_leads_to_list`, `remove_leads_from_list`, `check_setup`. |
| `src/marketo_plugin/plugin.py` | The `MarketoPlugin` EDGE provider — client lifecycle, error mapping, EDGE registration. |
