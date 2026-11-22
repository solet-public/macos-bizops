# Salesforce Plugin (`salesforce_plugin`)

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:salesforce, domain:local-solet, domain:cloud-solet

Embedding Description: Salesforce plugin reference — connecting many Salesforce orgs at once through the operator's sf CLI (binding orgs the CLI is logged in to, browser or unattended JWT login, the default org and the org argument every verb takes), the org lifecycle (scratch orgs from a Dev Hub, sandboxes from a production org), the verbs with argument shapes (SOQL, record CRUD, Bulk API v2, the typed families and the generic request), typed sf.* errors with recovery, and the full-CRUD-including-delete posture.

A full read/write connector over the operator's Salesforce orgs. Executor:
full CLI delegation — every verb shells out to the operator's `sf` CLI
directly (operator-ratified 2026-07-14, replacing the sf-CLI session-borrow
client factory, which is dead on current CLI releases: `sf org display
--json` now redacts `accessToken` unconditionally, verified live against CLI
2.142.7 — there is no longer a way to borrow a usable token from CLI output
at all). This is the enterprise-governance-friendly mode — it rides the
org's already-blessed Salesforce CLI Connected App, so enabling the connector requires
NO new org artifacts, no change request, and no admin ceremony. The solet acts as
the operator's own user in each org; its access to an org dies with the
CLI's login to it (logout/revocation) — both the feature and the operational
caveat. Many orgs are bound at once since build 6 (production orgs,
sandboxes, scratch orgs and Dev Hubs), each named by an alias; see "Many
orgs" below.

## Read/write posture — FULL CRUD INCLUDING DELETE (operator-ratified, RATIFY-2)

The operator's dividing line: *"document/ticket deletion is an acceptable
loss class, database destruction is not."* Salesforce is a SaaS workflow
tool, not the developer database connectors are — so, unlike
`snowflake_plugin`/`external_postgres_plugin`, this connector ships **full
CRUD including `delete_record`**, **Bulk API v2 ingest**
(`bulk_ingest_submit`/`bulk_job_status`/`bulk_job_results`/`bulk_job_abort`;
insert/update/upsert/delete/hardDelete — see "Bulk API v2 ingest" below), and
the typed families from build 3 (2026-11-07): `composite` (composite, graph,
batch, tree and sObject Collections, deletes included), `tooling` (Tooling
API queries, Tooling record writes and deletes, anonymous Apex and Apex test
runs) and `bulk_jobs` (the Bulk API 2.0 query job lifecycle plus ingest job
list and delete), and from build 4: `reports` (run, clone, save and delete
reports, asynchronous runs, report types), `dashboards` (results, refresh,
clone, save and delete), `rest_resources` (limits, recent items, object info
and describe, updated/deleted ids, records by external id including upsert
and delete, relationship traversal, paged query and queryAll, explain, SOSL
and parameterized search) and `actions` (invocable action discovery and
invocation: Apex, flows, quick actions, email alerts and standard actions),
and from build 5: `metadata` (Metadata API deployments over the REST
deployRequest resource, including check-only validation, destructive
changes, cancel and quick deploy), `content_files` (Salesforce Files upload,
size- and checksum-verified download, and ContentDocument delete), Tooling
composite and apexCompileResults, Bulk parallel result pages, report
instance delete, and sObject relationship update and delete, and from build
6: many bound orgs (`list_orgs`, `bind_org`, `login`, `logout`,
`set_default_org`, `disconnect_org`) and `org_lifecycle` (scratch org and
sandbox create, clone, refresh, resume, status and delete, plus org list,
display and open).
`authenticated_request` reaches every other REST endpoint. Salesforce's own
permissions on each bound user are the only boundary (USER ruling
`rul_4c99a30d`): the plugin adds no read-only mode, role request or
confirmation prompt, and it answers the CLI's own confirmations with
`--no-prompt`. The remaining typed gaps are listed in
`plugins/salesforce_plugin/knowledge_base/vendor_api_coverage.md`: SOAP-only
calls (Partner merge, convertLead, undelete, emptyRecycleBin; Metadata
retrieve, describeMetadata, listMetadata, readMetadata) need the access token
in the SOAP SessionHeader, which this plugin never holds, Platform Event
subscription needs a streaming transport, and the CLI's source and packaging
commands are build 7.

## Executor model — full CLI delegation; the CLI keeps every org's session

The durable credential of each org is the **sf CLI's own session for it**
(a keychain-backed refresh token after a browser login; for a JWT login the
CLI signs a new token from the key file whenever the session needs one).
The plugin never holds a session. Its vault holds the index of bound orgs
and, only for orgs that log in unattended, the login secrets the operator
stored (see "Many orgs"). Every verb shells out to the `sf` CLI directly with
`--target-org <the bound org's username>` — never `simple-salesforce`, never
a Python HTTP client, and no Salesforce SDK dependency at all. Three CLI
surfaces are in play:

- **Envelope commands** (`data query/get/delete`, `sobject describe`, `org
  display`) — the stable, non-beta CLI surface, invoked with `--json` for a
  clean `{status, result}` envelope. Used for every verb that only needs an
  id or a fixed path (`get_record`, `delete_record`, `describe_sobject`,
  `soql_query`, plus the org-binding verification call).
- **`api request rest`** (beta) — a generic authenticated REST passthrough
  with a JSON body file (`--body @file`). Used for `create_record` and
  `update_record` because the stable `data create/update record --values`
  command's field-value mini-language (`stringToDictionary` in the CLI's own
  `dataUtils.js`) has a genuine correctness bug: it silently coerces any
  field value that case-insensitively equals `"true"`/`"false"` into a
  boolean, and attempts a bare `JSON.parse` on any value containing both `{`
  and `}` — both are real data-corruption hazards for ordinary business data
  (e.g. an Account literally named "True Value Hardware"), not an escaping
  inconvenience a caller can quote around. `list_sobjects` also uses this
  surface (GET the global describe endpoint) because the stable `sobject
  list` command returns bare object names with no `label` field. A
  ContentVersion download uses its `--stream-to-file` mode, which writes the
  raw bytes and prints no status, so the download is kept only when it
  matches the version's `ContentSize` and MD5 `Checksum`.
- **File-based SOQL** — `soql_query` writes the query text to a tempfile and
  runs `data query --file <path>` (the operator's proven work-script
  pattern), keeping SOQL text out of the process argv `ps` would otherwise
  expose. No manual pagination: `sf data query` runs on jsforce with
  `autoFetch: true` internally, so it already collects every page up to a
  fetch cap before returning; `SF_ORG_MAX_QUERY_LIMIT` is passed as a
  subprocess env override to cap that fetch at the effective row limit
  server-side (500 by default, up to 1000 with an acknowledged override —
  see the verb map below), and the result is sliced to that same limit
  again client-side as defense-in-depth.

Each bound org's host pin is verified exactly ONCE per process lifetime,
lazily, on its first call (`client.py::SalesforceCliExecutor._verify_org_binding`
via `org display --json`; the plugin keeps one executor per bound org) and
cached — there is no rebuild-on-expiry mechanism, because the CLI refreshes
its own credential transparently inside every invocation. A fault is
therefore always either a CLI-level failure (binary missing, timeout, no live
session for the org) or a classified REST-level rejection
(`SalesforceCliCallError`, built from the CLI's `--json` error envelope or
the `api request rest` raw error array) — never a mid-flight expired session
this process must detect and retry. Every CLI call runs with
`SF_TEMP_SHOW_SECRETS=false` in its environment, whatever the solet's own
environment says, so the CLI keeps access tokens, passwords and SFDX auth
URLs masked in its output.

## One-time operator setup (runbook — proven live 2026-07-14, origin -> Branch org)

**This needs a Salesforce user account, nothing more.** No new Connected
App, no certificates, no admin change request: the flow rides the org's
already-blessed Salesforce CLI app, exactly like the operator's own `sf`
usage. The solet acts as the operator's user — its permissions are
that user's permissions, and its audit trail shows that user.

### Stage 1 — provision the sf CLI on consent (agent-executed)

1. Call `plugin::salesforce_plugin::provision_cli` with
   `acknowledge_system_change: true`. It probes first; when sf is missing it
   authorizes only `brew install sf` and Homebrew's declared `node` closure,
   refusing upgrades, reinstalls, removals, unlinks, and extra packages.
2. The process re-probes `sf --version`, persists the resolved absolute path
   as `sf_cli_path`, and rebinds the running plugin. It explicitly reports
   that this configuration update has no hidden reload or restart boundary.
   A compliant sf executable is not reinstalled.

### Stage 2 — log in and bind each org

3. Log in and bind in one step with `login` method `web` (the operator's
   browser opens; the job waits up to 5 minutes for the sign-in; SSO/MFA
   fine). Pass `flags.instance-url` (`https://<org>.my.salesforce.com`) only
   if the org mandates My Domain login, and `alias` to name the binding.
   Equivalently the operator runs `sf org login web --alias <alias>` once
   and the agent binds it with `bind_org {target_org: <alias>}`; the CLI
   keeps a keychain-backed refresh token and nothing re-prompts until logout
   or revocation.
4. Repeat for every org the solet should reach: a production org, its
   sandboxes, a Dev Hub. The first bound org is the default; `set_default_org`
   changes it, and `list_orgs` shows every binding.
5. A solet registered before build 6 has a `salesforce_org` address-book
   entry (`target_org` + `instance_host`); it needs no step: the first call of
   any verb binds that org under its `target_org` alias and says so.

### Stage 3 — enable + verify

6. If `salesforce_plugin` is not in the live manifest, add it via the
   blue-green manifest deploy (`apply_manifest` dry-run for the etag → CAS
   commit → wait for the swap; the entry point ships installed).
7. Dispatch `test_connection` (with `org` for a non-default org) → expect
   `{ok: true, org_id, username, api_version, org}` with the operator's own
   username.
8. Agent-driven setup note: `process_call` this plugin's verbs directly like
   any other process — they are not export-denied (the prior RATIFY-3 deny
   was retired by operator ruling 2026-07-15: friction, not security, on a
   single-user substrate — see
   workbench/2026-07-15_result_error_processing_architecture_deep_dive.md).

### Unattended login (JWT), for a solet with no one at the browser

9. In the org, create a connected app with a certificate (the operator's
   own key and certificate, e.g. from OpenSSL), pre-authorize the user, and
   note its consumer key.
10. Store the private key in the vault without it passing through a
    conversation: `service_interface::vault_service::store_from_file` with
    `key` `<solet>.salesforce_plugin.org_secret__<name>` and the key file's
    path. The key file can then be deleted.
11. `login` method `jwt`, `jwt_key_secret: <name>`, `flags: {username,
    client-id, instance-url}` (instance-url is the login URL, e.g.
    `https://login.salesforce.com` or `https://test.salesforce.com`). The key
    is written to a 0600 file only while a CLI call that may reach that org
    runs, because the CLI reads it again whenever it renews the session: the
    org's own calls, `org list`, and the `org display` that identifies an org
    being bound (those two leave out, with a warning, a key the vault no
    longer holds). Scratch orgs and sandboxes created from that org use the
    same key. An SFDX auth URL (`sfdx_auth_url_secret`) or an access token
    (`access_token_secret`) stored the same way are the other unattended
    methods.

### Recovery — expected failure modes

- `sf.unknown_org` — `org` names no bound org, or several orgs are bound and
  none is the default: the message lists the bound orgs; name one as `org`
  or run `set_default_org`.
- `sf.auth_failed` — no live CLI session for the org (never logged in,
  `logout`, `sf org logout`, or an admin revoked the token): log in again
  with `login`, or `sf org login web --alias <alias>`. The error message
  carries this hint.
- `sf.not_configured` naming `sf_cli_path` — the CLI binary is missing or
  the platform can't see it: reinstall the standalone bundle and/or fix the
  pinned path in the plugin config.
- `sf.not_configured` naming both instance hosts — the CLI target of a
  binding resolves to a different org than the binding's `instance_host`
  pin: bind the org again (`bind_org`) or fix the CLI alias; the plugin
  refuses to connect until they match.
- `sf.not_configured` with "No Salesforce org is bound" — bind one with
  `bind_org` or `login`.
- `sf.session_expired` — rare under full CLI delegation (the CLI refreshes
  its own credential inside every invocation); it indicates the org itself
  rejected an in-flight call, most likely because the CLI's refresh token
  died mid-session (org-side revocation or password change). One browser
  re-login restores everything; nothing platform-side to rotate.
- Credential lifecycle caveat (by design): the solet's Salesforce access
  is bound to the CLI's logins on THIS machine. A logout or org-side
  revocation turns that org off until its next login — the fully-supported
  dormant state, not a breakage; other bound orgs keep working.

## Many orgs (build 6)

Many orgs are bound at once, on the pattern of the Google plugin's multiple
accounts. One vault value, `<solet>.salesforce_plugin.orgs_index`, holds every
binding and the default org, written whole. A binding is keyed by its org id
and carries a unique alias, the username the CLI is called with, the pinned
instance host, the org type (production, sandbox or scratch), whether it is a
Dev Hub, how it logged in, and the names (never the values) of the vault
secrets its login used.

- **Every verb that reaches an org takes an optional `org`**: an alias, a
  username, or the 18- or 15-character org id. With one bound org it can be
  left out; with several the default is used. Every result names the org it
  ran against (`org` = alias, `org_id`). An `org` that matches no binding, or
  several bindings with no default, is `sf.unknown_org` listing the bound
  orgs. The org is resolved on the job worker, so dispatch stays a
  millisecond job write.
- **A binding is what the CLI and the org report**, never what a caller
  asserts: `org display` gives the org id, username and instance URL (whose
  host becomes the pin; an `instance_host` passed to `bind_org` must equal
  it), an Organization query says whether it is a sandbox, and the CLI's own
  Dev Hub test (`SELECT Id FROM ScratchOrgInfo`) says whether it is a Dev
  Hub. Binding an org again, by any name the CLI knows it by, refreshes the
  binding with that org id and keeps how it logged in; the name given is
  never matched against binding names. No alias, username or CLI target of
  one binding may name another bound org.
- **Login** (`login`) runs the CLI's own commands with every documented flag:
  `web` (browser), `jwt` (unattended; the key from the vault secret named in
  `jwt_key_secret`, or the `jwt-key-file` flag), `sfdx_url` (an SFDX auth URL
  from `sfdx_auth_url_secret`, sent on standard input, or the
  `sfdx-url-file` flag) and `access_token` (from `access_token_secret`,
  passed in `SF_ACCESS_TOKEN` to that one call; the session ends when the
  token expires). The CLI has no client-credentials login; a token minted
  that way elsewhere can be stored and used with `access_token`.
- **`logout`** ends the CLI's session for one org and keeps the binding;
  **`disconnect_org`** logs out, removes the binding, and deletes the vault
  secrets no remaining binding uses (a scratch org or sandbox created from a
  JWT-bound org shares that org's key). If the default is disconnected, the
  first remaining org becomes the default.
- **Secrets are never parameters, results or log lines** (USER, clause 22
  of `rul_e9a942a3`: "There should be no opportunity for passwords to be in
  logs, and no redaction should be needed."). The operator stores each one
  once with `service_interface::vault_service::store_from_file` under
  `<solet>.salesforce_plugin.org_secret__<name>`; verbs take only `<name>`.
  The value is read inside the call that hands it to the CLI and nothing is
  masked: results are built from the fields that describe an org, and when a
  login carried a vault secret a failure reports only the CLI's error name
  and a fixed hint, never the CLI's message.
- **Migration**: the `salesforce_org` address-book entry of earlier builds is
  bound on first use under its `target_org` alias (copied, not moved); it is
  deleted only when that org is disconnected.

## Org lifecycle (build 6)

`org_lifecycle` runs the CLI's org commands; `flags` takes every documented
flag of each command, and the plugin sets `--target-org`,
`--target-dev-hub`, `--async`, `--wait`, `--poll-interval` and `--no-prompt`
itself, refusing them from callers with the reason.

- **Scratch orgs**: `create_scratch` submits `sf org create scratch --async`
  to a bound Dev Hub (`org`; by default the only bound Dev Hub, else the
  default org when it is one) with a `definition-file` flag or an inline
  `definition` object, and returns the ScratchOrgInfo request (`job_id`).
  `resume_scratch` (`job-id` or `use-most-recent`) checks once: `ready: false`
  while the org is New or Creating; when it is ready the CLI authorizes it,
  deploys its settings, and the plugin binds it (`alias`, `make_default`).
  `delete_scratch` deletes a bound scratch org and its binding. Org limits,
  including DailyScratchOrgs and ActiveScratchOrgs, are `rest_resources`
  operation `limits` with `org` naming the Dev Hub; the Dev Hub's
  ActiveScratchOrg and ScratchOrgInfo records are ordinary SOQL there.
- **Sandboxes**: `create_sandbox` (name, license-type), `clone_sandbox`
  (source-sandbox-name or source-id) and `refresh_sandbox` submit the CLI's
  sandbox commands `--async` on a bound production org; the CLI inserts or
  updates the Tooling SandboxInfo. `resume_sandbox` checks the SandboxProcess
  once (`ready: false` with CopyProgress while it copies) and on completion
  the CLI logs in to the sandbox and the plugin binds it. `sandbox_status`
  reads SandboxProcess for a `sandbox_name` through the Tooling query
  resource. `delete_sandbox` deletes a bound sandbox the CLI created.
- **Every org**: `list` (`sf org list`, every org the CLI knows, cut to the
  fields that describe each org), `display` (one bound org; never its access
  token, password or SFDX auth URL) and `open` (opens the org in this
  machine's browser, or with `url-only` returns the CLI's single-use
  frontdoor link).

Creates are always submitted `--async` and resumes check once (`--wait 0`):
a scratch org takes minutes and a sandbox can take hours, and the plugin's
one job worker must not be held that long — the same reason Bulk ingest is
submitted `--async`. A create returns `next`, the resume call to make.

A live smoke, `plugins/salesforce_plugin/tests/live_scratch_org_smoke.py`, creates and deletes a real
scratch org through these verbs when `SALESFORCE_LIVE_DEV_HUB` names a Dev
Hub the local CLI is logged in to; in a smoke battery it reports a disclosed
skip.

## Verbs (built — 32 total)

Every verb below except `list_orgs`, `bind_org`, `login`, `set_default_org`,
`probe_cli` and `provision_cli` also takes `org` (optional: an alias,
username or org id of a bound org; the default org when omitted), and its
result also carries `org` (the alias) and `org_id`. `logout` and
`disconnect_org` require `org`.

| Verb | Args | Returns |
|---|---|---|
| `list_orgs` | — | `{orgs:[{org_id, alias, target_org, username, instance_host, org_type, is_dev_hub, login_method, dev_hub_org_id, jwt_key_secret, secret_names, migrated_from_legacy, default}], count, default_org, legacy_pending_migration}` |
| `bind_org` | `target_org`, `alias?`, `instance_host?`, `make_default?` | `{binding, org, org_id}` — an org the CLI is logged in to |
| `login` | `method` (web, jwt, sfdx_url, access_token), `flags?`, `alias?`, `make_default?`, `jwt_key_secret?`, `sfdx_auth_url_secret?`, `access_token_secret?` | `{method, binding, org, org_id}` |
| `logout` | `org`, `flags?` (client-app) | `{logged_out_usernames, binding_kept}` |
| `set_default_org` | `org` | `{org, org_id, default}` |
| `disconnect_org` | `org` | `{org, org_id, logout_status, binding_removed, secrets_deleted, legacy_entry_deleted, default_org}` |
| `org_lifecycle` | `operation`, `org?`, `flags?`, `definition?`, `sandbox_name?`, `connected_app_secret?`, `alias?`, `make_default?` | per operation — list, display, open, create_scratch, resume_scratch, delete_scratch, create_sandbox, clone_sandbox, refresh_sandbox, resume_sandbox, sandbox_status, delete_sandbox (see "Org lifecycle") |
| `test_connection` | — | `{ok, org_id, username, api_version}` |
| `soql_query` | `query`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?`, `include_deleted=false` | `{path, columns, row_count, total_size, truncated}` — written as ONE `.tsv` file at the caller's ABSOLUTE path, never records inline; defaults to 500 records, up to 1000 with an acknowledged override |
| `export_soql` | `query`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?`, `include_deleted=false` | `{path, columns, row_count, total_size, truncated}` — the N>>500 route: same shape as soql_query, defaults to 500 records, up to 50000 with an acknowledged override |
| `get_record` | `sobject`, `id`, `fields?` | `{record}` |
| `describe_sobject` | `sobject` | `{fields:[{name, type, label, nillable, updateable}]}` |
| `list_sobjects` | — | `{sobjects:[{name, label}]}` |
| `create_record` | `sobject`, `fields` | `{id, success}` |
| `update_record` | `sobject`, `id`, `fields` | `{success}` |
| `delete_record` | `sobject`, `id` | `{success}` (permanent — see read/write posture) |
| `bulk_ingest_submit` | `operation`, `sobject`, `csv_path`, `external_id_field?` | `{job_id, state, processed_records, failed_records, error_message}` — submits a Bulk v2 ingest job `--async`; `csv_path` is read-contained under a saved location |
| `bulk_job_status` | `job_id` | `{job_id, state, processed_records, failed_records, error_message}` — the poll target for a submitted job |
| `bulk_job_results` | `job_id`, `successful_results_path`, `failed_results_path`, `unprocessed_results_path` | `{successful_results_path, failed_results_path, unprocessed_results_path}` — three CSVs written under a saved location |
| `bulk_job_abort` | `job_id` | `{job_id, state, processed_records, failed_records, error_message}` |
| `composite` | `operation`, `body?`, `query?`, `headers?`, `sobject?`, `external_id_field?` | `{family, operation, method, status, headers, body, correlation_id, error_code?, failures?}` — composite, graph, batch, tree, collections_create/retrieve/retrieve_many/update/upsert/delete |
| `tooling` | `operation`, `query?`, `body?`, `headers?`, `sobject?`, `id?`, `next_records_url?` | same shape plus `pagination.next_records_url` — query, query_more, search, execute_anonymous, run_tests_async/sync, completions, list_sobjects, sobject_info, describe, create/get/update/delete, composite_resources, composite, apex_compile_results (API 68.0+, empty body) |
| `bulk_jobs` | `operation`, `job_id?`, `query?`, `body?`, `headers?`, `output_csv_path?` | same shape; `query_results` writes one CSV page under a saved location and returns `output_csv_path`, `bytes_written`, `pagination.next_locator` — query_create/get/list/results/abort/delete, query_result_pages (`pagination.result_locators` for parallel pages) and query_result_pages_more (its `nextRecordsUrl`), ingest_list/delete |
| `reports` | `operation`, `report_id?`, `instance_id?`, `report_type?`, `query?`, `body?`, `headers?` | same shape plus `pagination.report_all_data` (false when a run returned only the vendor's first detail rows) — list, create, clone, describe, run, run_filtered, run_async, instances, instance_results, instance_delete, query, update, delete, report_types, describe_report_type |
| `dashboards` | `operation`, `dashboard_id?`, `query?`, `body?`, `headers?` | same shape — list, clone, describe, results, refresh, status, update, delete |
| `rest_resources` | `operation`, `sobject?`, `external_id_field?`, `external_id?`, `id?`, `relationship?`, `next_records_url?`, `query?`, `body?`, `headers?` | same shape plus `pagination.next_records_url` — resources, limits, recent, describe_global, sobject_info, describe, updated, deleted, get/upsert/delete_by_external_id, relationship, relationship_update, relationship_delete, query, query_all, query_more, explain, search, parameterized_search |
| `actions` | `operation`, `action_type?`, `action?`, `body?`, `headers?` | same shape; `failures` counts invocation results whose `isSuccess` is false — list, list_standard, list_custom, list_custom_type, describe_standard, describe_custom, invoke_standard, invoke_custom |
| `metadata` | `operation`, `deploy_request_id?`, `zip_path?`, `query?`, `body?`, `headers?` | same shape; `failures` counts component and test errors of a Failed or SucceededPartial deployment — deploy (multipart `.zip` read under a saved location, `deployOptions`), deploy_status, deploy_cancel, deploy_quick |
| `content_files` | `operation`, `content_version_id?`, `content_document_id?`, `file_path?`, `output_path?`, `body?`, `headers?` | same shape; a download adds `output_path`, `bytes_written`, `md5` and `integrity_advisory` when the vendor size or checksum was unreadable — upload (file read under a saved location becomes base64 `VersionData`), download (written under a saved location), delete_document |
| `authenticated_request` | `method`, `path`, `query?`, `headers?`, `body?` | `{status, headers, body, correlation_id, nextRecordsUrl?}` — any other relative endpoint on the bound org |
| `probe_cli` / `provision_cli` | — / `acknowledge_system_change` | local `sf` CLI diagnostic and consent-gated install |

Both `soql_query` and `export_soql` ALWAYS write their result to the
caller's `output_tsv_path` — never records inline, at any size
(business-data limits + data-export migration, 2026-08-02; the former
inline-return/byte-cap branch is deleted, not lowered). Neither destination
is platform blob storage (operator ruling 2026-07-15); the path must be
absolute, end in `.tsv`, and lie under a saved path address, the session launch folder or the solet home. The shared guard excludes the installation tree. Nested relationship objects serialize as JSON text in
their cells. Each defaults to 500 records absent an acknowledged override;
`soql_query`'s override ceiling is 1000, `export_soql`'s is 50000 (the
N>>500 route). `acknowledge_default_limit_override=true` together with an
explicit `row_limit` requests more than the default — both are required
together, and a `row_limit` above the verb's hard cap is refused, never
silently clamped. `include_deleted=true` runs the query through the vendor
`queryAll` resource (`sf data query --all-rows`), which also returns deleted
records still in the recycle bin and archived records. Neither verb has a
vendor-imposed ceiling to defer to: these verbs never run SOQL inside Apex,
so the 50,000-record Apex governor limit does not apply; the actual Salesforce fact for this call path is a
2,000-record REST query batch size with no vendor total ceiling, and
jsforce's `autoFetch` already pages past that internally.

## Bulk API v2 ingest and query jobs

Four verbs cover the write side of Bulk API v2 — submitting large insert/
update/upsert/delete jobs from a CSV file, polling them, fetching their
result CSVs, and aborting them. All four are, like every verb in this
plugin, on the D0.3 deferred-completion shape: the dispatch returns a
plugin-level `job_id`/`status: queued` in milliseconds, and the *Salesforce*
Bulk job's own id/state is delivered separately when that dispatch's job
completes — two independent async layers, never conflate the two ids.

- **`bulk_ingest_submit`** runs `sf data <operation> bulk --async --json` and
  returns the instant Salesforce ACKNOWLEDGES the job (state `Open` or
  similar) — never `--wait`, because a Bulk v2 job can run minutes, well past
  this connector's 30s per-call subprocess bound (`SF_CLI_TIMEOUT_SECONDS`).
  `operation` is one of `insert`/`update`/`upsert`/`delete`/`hardDelete`.
  `hardDelete` runs `sf data delete bulk --hard-delete`: the records bypass the
  recycle bin, and Salesforce itself refuses the job unless the user holds the
  "Bulk API Hard Delete" system permission — the vendor's permission is the
  boundary, the plugin adds none of its own. `external_id_field`
  is required if, and only if, `operation` is `upsert` — supplying it for any
  other operation is refused, not silently ignored. `csv_path` must be an
  ABSOLUTE path already on disk, contained under a saved path address, the session folder or solet home.
- **`bulk_job_status`** is the poll target: `GET /jobs/ingest/{id}` via
  `run_rest`, an honest passthrough of the Salesforce job's `state`
  (`Open` → `UploadComplete` → `InProgress` → `JobComplete`/`Failed`/
  `Aborted`) plus processed/failed record counts.
- **`bulk_job_results`** fetches the job's three result CSVs — successful,
  failed, and unprocessed records — and writes each to a caller-supplied
  ABSOLUTE path under a saved location (the same shared containment `export_soql`
  uses, admitted with a `.csv` suffix instead of `.tsv`). The CSV bodies come
  back via `run_rest`'s raw-text mode rather than JSON-decoded — the measured
  design choice over the CLI's own `sf data bulk results --job-id`, which
  writes into its own resolved cwd outside this plugin's containment
  boundary; REST + our own contained file write keeps every byte this verb
  writes inside the same gate `export_soql` already uses.
- **`bulk_job_abort`** does `PATCH /jobs/ingest/{id}` with `state: Aborted` —
  the recovery lever for a job submitted in error. Records already processed
  before the abort are NOT rolled back.
- **`bulk_jobs`** covers the rest of Bulk API 2.0 over the same transport as
  `authenticated_request`: `query_create` (`POST /jobs/query` with operation
  `query` or `queryAll`), `query_get`, `query_list` and `ingest_list` (job
  lists, paged by `queryLocator`), `query_results` (`GET
  /jobs/query/{id}/results` with `locator` and `maxRecords`; each page is
  written as a CSV under a saved location, never inline, and the
  `Sforce-Locator` header comes back as `pagination.next_locator`),
  `query_abort`, `query_delete` and `ingest_delete`.

Errors are typed with the `sf.*` prefix: `sf.not_configured`,
`sf.invalid_params`, `sf.auth_failed`, `sf.session_expired`,
`sf.permission_denied`, `sf.not_found`, `sf.malformed_query`,
`sf.rate_limited`, `sf.api_error`, `sf.export_path_refused`,
`sf.import_path_refused`, `sf.unknown_org` (no bound org matches, or several
and no default) and `sf.org_registry_failed` (the vault index of bound orgs
could not be read or written). The typed families report vendor HTTP failures as
completion data with an `error_code`, adding `sf.bad_request` (400),
`sf.conflict` (409) and `sf.precondition_failed` (412) to the classes above.

## Security posture (mirrors the Jira/Snowflake/external-Postgres wave)

- **Foreign-target invariant.** The plugin reaches only bound orgs: `org`
  must name a binding (an unknown one is refused with the list), every CLI
  call is made with that binding's username, and the resolved instance host
  must equal the binding's `instance_host` pin exactly or the verification
  refuses (the CLI's alias cache can hold many orgs; the pin guarantees each
  call reaches the org that was bound). A binding is made from what the CLI
  and the org report, never from what a caller asserts.
- **No export deny — direct `process_call` is the normal path.** Retired by
  operator ruling 2026-07-15: on this single-user substrate every MCP session
  is the operator, so the deny was friction, not security — it forced the
  manual `sf --json` shell-out that routed 200 records through context in the
  2026-07-14 Blocked_Email_Domain incident. See the ruling doc above.
- **Generic error messages for topology-leaking classes.** Auth, session
  expiry, and permission errors NEVER embed the raw driver exception string
  (it can carry the org's my-domain host). Not-found and malformed-query
  errors describe the caller's own record/query and may keep response-body
  detail.
- **Secrets hygiene.** The durable credential of each org is the sf CLI's
  own session; the plugin never holds one. It declares two vault keys
  (`get_declared_vault_keys`): the org index, and the
  `org_secret__*` prefix under which the operator stores a login secret for
  an org that logs in unattended (none is required at readiness). A secret
  is never a parameter (the job ledger stores parameters), argv, a result or
  a log line: it goes to the CLI as a 0600 key file that exists only while
  a call that may reach that org runs, on standard input, or in the
  environment of the one login call. Every CLI call runs with
  `SF_TEMP_SHOW_SECRETS=false`, and results are built from the fields that
  describe an org (`org display` and `org list` output never contributes its
  access token, password, SFDX auth URL, client id or key path). A key file
  left by a call killed mid-flight is removed when the plugin starts. The
  org index records secret names, never values.
- **Results and log hygiene.** Results are not scanned. The connector returns
  the vendor result as received; bulk results go to disk through
  `output_path`. Audit and log lines are built only from method, path without
  query values, status, duration and correlation id, so they never carry
  headers, bodies or credential values.

## SQL-lockdown gate note

SOQL strings like `SELECT Id, Name FROM Account` match the gate's S2
verb+FROM heuristic even though they target the FOREIGN Salesforce org — a
false positive the gate cannot resolve statically. One whole-file
`sanctioned-exempt` entry in `quality_gates/sql_access_allowlist.txt` covers
`soql_actions.py` (the only module composing SOQL-shaped text); `record_actions.py`,
`client.py`, `errors.py`, and `plugin.py` stay fully gated.

## Key files

| File | Purpose |
|---|---|
| `src/salesforce_plugin/constants.py` | Every magic value: the vault keys of the bound orgs, address-book field names, error codes, result types, caps. |
| `src/salesforce_plugin/app_config.py` | Reads the pre-build-6 `salesforce_org` entry (target_org + instance_host, both literal) with ordinary resolve; the source of the first binding's migration. |
| `plugins/salesforce_plugin/src/salesforce_plugin/orgs.py` | The vault index of bound orgs: bindings keyed by org id with an alias, the default, selection by alias, username or org id, and `sf.unknown_org`. |
| `plugins/salesforce_plugin/src/salesforce_plugin/org_service.py` | Resolving the org a call runs as (with the legacy migration), binding from `org display` and the org's own facts, login, logout, set default and disconnect. |
| `plugins/salesforce_plugin/src/salesforce_plugin/org_secrets.py` | Operator-stored login secrets: read inside the one call that uses them; a JWT key as a 0600 file only during calls that may reach its org (a key the vault no longer holds is left out of `org list` and of the display that identifies an org being bound, with a warning). |
| `plugins/salesforce_plugin/src/salesforce_plugin/cli_commands.py` | The documented flags of each wrapped sf command, caller flags versus plugin-set flags with reasons, and argv in `--flag=value` form. |
| `plugins/salesforce_plugin/src/salesforce_plugin/org_lifecycle.py` | `org_lifecycle`: org list, display and open, scratch orgs from a Dev Hub, sandboxes from a production org, and binding new orgs when they are ready. |
| `src/salesforce_plugin/client.py` | `SalesforceCliExecutor`, one per bound org — `run_json()` (envelope commands) + `run_rest()` (`api request rest` with a JSON body file, or `raw_response=True` for a CSV body — `bulk_job_results`); verifies its org's host pin once, lazily, via `org display --json`; every child process gets `SF_TEMP_SHOW_SECRETS=false`. No rebuild-on-expiry — the CLI manages its own credential refresh. |
| `src/salesforce_plugin/errors.py` | Topology-safe error classification (`classify_salesforce_error`) over `SalesforceCliCallError` (error_code + detail_message from the CLI's `--json` error envelope or the `api request rest` raw error array). |
| `src/salesforce_plugin/soql_actions.py` | `soql_query` — file-based query, `SF_ORG_MAX_QUERY_LIMIT` env cap + client-side slice; no manual pagination (the CLI autofetches). |
| `src/salesforce_plugin/record_actions.py` | `get_record`/`describe_sobject`/`delete_record` via stable ID-based commands; `create_record`/`update_record`/`list_sobjects` via `run_rest` (the `--values` mini-language's `stringToDictionary` parser has a proven correctness bug — see the executor model section above). |
| `plugins/salesforce_plugin/src/salesforce_plugin/bulk_actions.py` | Bulk API v2 ingest — `bulk_ingest_submit` (`data <op> bulk --async`), `bulk_job_status`/`bulk_job_abort` (`run_rest` against `/jobs/ingest/{id}`), `bulk_job_results` (raw-text `run_rest` → three contained CSV writes). |
| `ananta/src/ananta/file_locations/containment.py` | Shared read/write containment from saved path addresses, the session folder and solet home. Ingest inputs must already exist; the installation tree is excluded. |
| `plugins/salesforce_plugin/src/salesforce_plugin/request_actions.py` | `authenticated_request`: one forwarded call returning the vendor result as received, an audit line built only from method, path without query values, status, duration and correlation id, plus the typed-only media proof for the CLI's header/body boundary. |
| `plugins/salesforce_plugin/src/salesforce_plugin/transfers.py` | The local-file operations: the multipart Metadata deploy package (`saved locations`, `.zip`), the base64 ContentVersion upload and the `--stream-to-file` download proven against `ContentSize` and `Checksum` (`saved locations`). |
| `plugins/salesforce_plugin/src/salesforce_plugin/typed_families.py` | The `composite`, `tooling`, `bulk_jobs`, `reports`, `dashboards`, `rest_resources`, `actions`, `metadata` and `content_files` operation tables (vendor method and path per operation), input checks, error-code mapping, failure counts and pagination hints. |
| `src/salesforce_plugin/plugin.py` | The `SalesforcePlugin` EDGE provider — error mapping, EDGE registration. No retry wrapper: a CLI invocation either succeeds or fails classified, never a stale-session mid-flight fault to retry. |
