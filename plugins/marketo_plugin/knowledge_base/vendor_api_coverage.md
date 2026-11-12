# Marketo vendor API coverage

Article Layer: 1

Article Role: capability_reference

Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:marketo, domain:api-inventory

Embedding Description: Inventory of the Marketo REST Lead Database, Assets, Activities, Bulk Extract/Import, Identity, Lists, and generic-request coverage against the current marketo_plugin verbs. It records verified paths, gaps, posture and guard sites, and the build order for full vendor-RBAC-bounded coverage.

## Purpose and boundary

This is the implementation specification for the next marketo_plugin build
units. It compares the Marketo surface named by Feature Request
`frq_6f9bf5196cdf4bb2b0ecc07b071414f4` with the verbs present in this checkout,
and each build updates the rows it closes. The connector must reach
everything the registered Marketo API user's role permits; vendor RBAC is the
authorization boundary. The plugin's foreign-target invariant still applies:
requests use only the one `marketo_instance` address-book entry and never accept
a caller-supplied host.

Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.

Protocol below means HTTPS JSON REST unless a row says otherwise. Paths marked
"to verify" are intentionally not guessed: confirm the exact endpoint, method,
request shape, and permission against the named Adobe Marketo Engage Developer
Guide before implementing the typed wrapper.

## Surface inventory

| Family and objects | Protocol and base path | What it does | Reads | Writes | Deletes | Current plugin coverage / next path |
|---|---|---|---|---|---|---|
| Lead Database: leads/persons | HTTPS REST, `/rest/v1/leads...` | Query and describe people; create, upsert, update, merge, and remove lead records | Yes | Yes | Yes | `get_leads`, `describe_lead_fields` (read); `create_or_update_leads`, `merge_leads` (write); `delete_leads` (destructive write). Exact existing paths are verified in `marketing_actions.py`; extend typed lifecycle coverage for any lead operation still absent. |
| Lead Database: companies | HTTPS REST, verified (Adobe Marketo Engage REST API — Companies): `GET /rest/v1/companies/describe.json`, `GET /rest/v1/companies.json`, `POST /rest/v1/companies.json` (sync; no `/sync.json` suffix), `POST /rest/v1/companies/delete.json` | Read and manage company records and their fields | Yes | Yes | Yes | Covered (build 2): `describe_companies`, `get_companies` (read); `sync_companies` (create/update/upsert, field clear by writing an empty value); `delete_companies` (destructive). Keys `externalCompanyId` / `id`. No vendor merge endpoint. |
| Lead Database: opportunities and opportunity roles | HTTPS REST, verified (Adobe Marketo Engage REST API — Opportunities, Opportunity Roles): `GET /rest/v1/opportunities/describe.json`, `GET`/`POST /rest/v1/opportunities.json`, `POST /rest/v1/opportunities/delete.json`; roles at `/rest/v1/opportunities/roles/describe.json`, `/rest/v1/opportunities/roles.json` (compound-key query `POST ...?_method=GET`) and `/rest/v1/opportunities/roles/delete.json` | Read and manage opportunity records and their lead relationships (roles) | Yes | Yes | Yes | Covered (build 2): `describe_`/`get_`/`sync_`/`delete_opportunities` and `describe_`/`get_`/`sync_`/`delete_opportunity_roles` (`get_opportunity_roles` also takes `compound_keys`, paged on the body `nextPageToken` since build 3). Keys `externalOpportunityId` / `marketoGUID`; roles `externalOpportunityId` + `leadId` + `role` / `marketoGUID`. No vendor merge endpoint. The field-schema browse paths (`/rest/v1/opportunities/schema/fields...`) remain on `request`. |
| Lead Database: sales persons | HTTPS REST, verified (Adobe Marketo Engage REST API — Sales Persons): `GET /rest/v1/salespersons/describe.json`, `GET`/`POST /rest/v1/salespersons.json`, `POST /rest/v1/salespersons/delete.json` | Read and manage sales-person records used by the Lead Database | Yes | Yes | Yes | Covered (build 2): `describe_sales_persons`, `get_sales_persons`, `sync_sales_persons`, `delete_sales_persons`. Keys `externalSalesPersonId` / `id`; the vendor skips deleting a sales person linked to active leads. |
| Lead Database: custom objects | HTTPS REST, verified (Adobe OpenAPI `swagger-mapi.json` and Adobe Marketo Engage REST API — Custom Objects): `GET /rest/v1/customobjects.json` (`names`), `GET /rest/v1/customobjects/{apiName}/describe.json`, `GET /rest/v1/customobjects/{apiName}.json` (compound key `POST ...?_method=GET`), `POST /rest/v1/customobjects/{apiName}.json`, `POST /rest/v1/customobjects/{apiName}/delete.json`; type schema under `/rest/v1/customobjects/schema...` | Read, create, update, deduplicate, and remove custom-object records; define and approve custom-object types | Yes | Yes | Yes | Records covered (build 3): `list_custom_object_types`, `describe_custom_objects`, `get_custom_objects` (also `compound_keys`, paged on the body `nextPageToken`), `sync_custom_objects`, `delete_custom_objects`, each taking `api_name`. Keys are the type's dedupe fields / `marketoGUID`. Gap (build 4): typed type-schema verbs (create/update type, approve, discardDraft, delete type, add/update/delete field, fieldDataTypes, linkableObjects, dependentAssets); `request` is the interim route. Bulk variants belong to Bulk Import/Extract below. |
| Lead Database: named accounts and named account lists | HTTPS REST, verified (Adobe OpenAPI `swagger-mapi.json` and Adobe Marketo Engage REST API — Named Accounts, Named Account Lists): `GET /rest/v1/namedaccounts/describe.json`, `GET`/`POST /rest/v1/namedaccounts.json`, `POST /rest/v1/namedaccounts/delete.json`; `GET`/`POST /rest/v1/namedAccountLists.json`, `POST /rest/v1/namedAccountLists/delete.json`; membership `GET`/`POST /rest/v1/namedAccountList/{id}/namedAccounts.json`, `POST .../namedAccounts/remove.json` | Read and manage named-account records, named account lists and list membership | Yes | Yes | Yes | Covered (build 3): `describe_`/`get_`/`sync_`/`delete_named_accounts` (keys `name` / `marketoGUID`); `get_`/`sync_`/`delete_named_account_lists` (sync createOnly/updateOnly only, no `fields` on the query); `get_`/`add_`/`remove_named_account_list_members`. No vendor merge endpoint. The field-schema browse paths (`/rest/v1/namedaccounts/schema/fields...`) remain on `request` (build 4). |
| Lists: static and smart lists | HTTPS REST static-list paths `/rest/v1/lists...`; Asset API smart-list paths `/rest/asset/v1/smartList...` | Discover lists and manage static-list membership; inspect smart-list rules where the entitlement permits | Yes | Yes (static membership and any vendor-supported list mutation) | Yes where the vendor exposes list deletion; verify | `list_static_lists` (read), `add_leads_to_list` and `remove_leads_from_list` (write). Smart-list reads and list object create/update/delete are gaps; use the generic path until exact Asset endpoints are verified. |
| Programs | HTTPS REST Asset API, `/rest/asset/v1/program...` (exact plural/path forms to verify against Adobe Marketo Engage REST API — Programs) | Read, create, clone, update, approve, archive, and delete programs and program membership configuration | Yes | Yes | Yes | Gap: typed program lifecycle and membership verbs; `request` is the immediate long-tail route. |
| Smart campaigns | HTTPS REST Lead API trigger `/rest/v1/campaigns/{id}/trigger.json`; Asset API `/rest/asset/v1/smartCampaign...` | Discover campaign metadata/rules; trigger campaigns; create, clone, update, approve, schedule/unschedule where vendor-supported, and delete campaign assets | Yes | Yes | Yes | `list_campaigns` (read) and `trigger_campaign` (write) cover the current Lead API. Asset metadata and lifecycle operations are gaps. Flow steps are not exposed by a published REST endpoint; do not invent a flow-read verb. |
| Smart-list rules | HTTPS REST Asset API, `/rest/asset/v1/smartCampaign/{id}/smartList.json` and `/rest/asset/v1/smartList...` | Read filters and triggers that determine campaign qualification | Yes | Yes where the Asset API exposes rule editing; verify | Yes where the vendor exposes smart-list deletion; verify | Gap. Existing KB verifies campaign smart-list read examples; the Read-Only Assets permission must be checked by the consumer; reachable through `request`. |
| Emails and email templates | HTTPS REST Asset API, `/rest/asset/v1/email...` and `/rest/asset/v1/emailTemplate...` (exact paths/methods to verify against Adobe Marketo Engage REST API — Emails and Email Templates) | Read, create, clone, update, approve, archive, and delete email assets/templates | Yes | Yes | Yes | Gap: typed asset lifecycle verbs, with generic request as the interim path. |
| Landing pages | HTTPS REST Asset API, `/rest/asset/v1/landingPage...` (exact paths/methods to verify against Adobe Marketo Engage REST API — Landing Pages) | Read, create, clone, update, approve, archive, and delete landing-page assets | Yes | Yes | Yes | Gap: typed asset lifecycle verbs, or `request`. |
| Forms | HTTPS REST Asset API, `/rest/asset/v1/form...` (exact paths/methods to verify against Adobe Marketo Engage REST API — Forms) | Read, create, clone, update, approve, archive, and delete form assets | Yes | Yes | Yes | Gap: typed asset lifecycle verbs, or `request`. |
| Files | HTTPS REST Asset API, verified Adobe Asset OpenAPI: `GET /rest/asset/v1/file/{id}.json`, `GET /rest/asset/v1/file/byName.json`, `GET`/`POST /rest/asset/v1/files.json`, `POST /rest/asset/v1/file/{id}/content.json` | Read metadata, upload/create/upsert files and images, replace content | Yes | Yes | No published delete/archive endpoint (Adobe Files guide and OpenAPI, 2026-11-07) | Covered (build 5): `get_asset_file`, `get_asset_file_by_name`, `list_asset_files`, `create_asset_file`, `update_asset_file_content`. Multipart file part includes filename, MIME type and base64 input bytes. Browse pages internally with next_offset if capped. |
| Tokens | HTTPS REST Asset API, verified Adobe Asset OpenAPI: `GET`/`POST /rest/asset/v1/folder/{id}/tokens.json`, `POST /rest/asset/v1/folder/{id}/tokens/delete.json` (form encoded) | Read, create, correct, clear and delete local My Tokens on folders/programs | Yes | Yes | Yes | Covered (build 4): `get_asset_tokens`, `set_asset_token` (create/upsert/correct; empty value clears), `delete_asset_token`. Every documented parameter is exposed. Vendor creation types: date, number, rich text, score, sfdc campaign, text. No independent approve/archive endpoint. |
| Folders | HTTPS REST Asset API, verified Adobe Asset OpenAPI: `GET`/`POST /rest/asset/v1/folders.json`; `GET /rest/asset/v1/folder/byName.json`; `GET`/`POST /rest/asset/v1/folder/{id}.json`; `GET .../{id}/content.json`; `POST .../{id}/delete.json` | Browse folders/content, read by id/name, create, rename/correct/clear description, archive/unarchive, delete | Yes | Yes | Yes | Covered (build 4): `list_asset_folders`, `get_asset_folder`, `get_asset_folder_by_name`, `get_asset_folder_content`, `create_asset_folder`, `update_asset_folder`, `delete_asset_folder`. All documented query/form parameters; browse/content page internally and return next_offset if the total row cap cuts a page. No published move/clone/approve endpoint; programs use their own lifecycle API. |
| Tags | HTTPS REST Asset API, `/rest/asset/v1/tag...` (exact paths/methods to verify against Adobe Marketo Engage REST API — Tags) | Read and manage asset tags and tag assignments | Yes | Yes | Yes where exposed | Gap: typed tag lifecycle/assignment; generic request interim. |
| Channels | HTTPS REST Asset API, `/rest/asset/v1/channel...` (exact paths/methods to verify against Adobe Marketo Engage REST API — Channels) | Read and manage program channels and channel metadata | Yes | Yes | Yes where exposed | Gap: typed channel lifecycle; generic request interim. |
| Activities and activity types | HTTPS REST, `/rest/v1/activities/types.json`, `/rest/v1/activities/pagingtoken.json`, `/rest/v1/activities.json` | Enumerate instance activity types and read activity history | Yes | No direct activity mutation in the named surface | No direct activity deletion in the named surface | `list_activity_types` and `get_activities` (read), plus `check_marketo_job_status` for async retrieval. Exact paths are verified in `constants.py`. Activity writes are represented by the source operation that caused them (lead/campaign writes), not fabricated as a separate activity-write API. |
| Bulk Extract: leads, activities, program members, custom objects | Verified Adobe Lead Database OpenAPI: `/bulk/v1/{leads,activities,program/members}/export.json`, `/export/create.json`, `/export/{exportId}/{enqueue,status,file,cancel}.json` | List/create/enqueue/status/cancel/download vendor extract jobs | Yes | Job control writes | Cancel is terminal; no published delete/reopen | Covered (build 5): `list_bulk_<family>_extract_jobs`, `create_bulk_<family>_extract`, `enqueue_bulk_<family>_extract`, `get_bulk_<family>_extract_status`, `download_bulk_<family>_extract`, `cancel_bulk_<family>_extract` for lead/activity/program_member. Lists preserve nextPageToken; downloads preserve Range/content_range and body_base64 as received. Custom-object extract typed verbs remain build 6, reachable through request. |
| Bulk Import: leads, program members, custom objects | Verified Adobe Lead Database OpenAPI: `POST /bulk/v1/leads.json`, `/customobjects/{apiName}/import.json`, `/program/{programId}/members/import.json`; lead status `/leads/batch/{batchId}.json`, custom-object status `/customobjects/{apiName}/import/{batchId}/status.json`, program-member status `/program/members/import/{batchId}/status.json`; failures/warnings under the batch resource | Multipart upsert submission (automatically queued), status and row reports | Yes | Yes | No published import cancel/delete/reopen; no published bulk activity import | Covered (build 5): `import_leads`, `import_custom_objects`, `import_program_members`, `get_<family>_import_status`, `get_<family>_import_failures`, `get_<family>_import_warnings` for lead/custom_object/program_member. All query/body parameters; batchId and terminal states/row errors retained by the platform job ledger. |
| Identity / OAuth token | HTTPS REST, `/identity/oauth/token` on the configured `base_url` | Mint an OAuth 2.0 client-credentials bearer token for subsequent requests | Token/credential response read | Token mint is an auth operation, not business-data mutation | No business-data delete | `test_connection` and the shared `MarketoClient` token path. Exact path is verified in `constants.py`; no new identity wrapper is required unless token introspection/revocation is later requested. |
| Generic authenticated request (long tail) | HTTPS REST, arbitrary path under the configured `base_url`; method GET/POST/PUT/PATCH/DELETE and JSON/form/multipart body | Reach a vendor endpoint before a dedicated typed wrapper exists, subject only to registered credential/RBAC and normal target invariant | Yes | Yes | Yes | Covered (build 1) as `request`: method/path/query/JSON or form body/headers, response envelope preservation, job-ledger dispatch. Multipart file bodies are covered (build 5) via multipart_files. It is the interim route for every gap row above, not a second unbounded verb per family. |

No row is classified as unsupported. A row marked “gap” means the current
plugin has no typed wrapper yet; its reachable route today is the generic
`request` verb, until a subsequent typed unit lands.
Endpoint details explicitly marked “to verify” must be checked against Adobe's
current endpoint reference before code is written.

## Current verb map and job-ledger behavior

The process KB under `knowledge_base/processes/` is the source for the current
verb names and their caller-facing contracts:

| Current verb | Kind | Family / operation |
|---|---|---|
| `test_connection` | diagnostic read/auth, async | Identity token mint and credential reachability |
| `check_setup` | diagnostic read, async | Six safe probes: lead schema/query, activity types, API usage, campaigns, static lists |
| `describe_lead_fields` | read, async | Lead field metadata and searchable fields |
| `get_leads` | read, async | Lead query and internal pagination |
| `create_or_update_leads` | write, async | Lead create/upsert/update modes |
| `delete_leads` | destructive write, async | Lead deletion |
| `merge_leads` | destructive write, async | Lead merge, including optional CRM merge |
| `list_static_lists` | read, async | Static-list discovery |
| `add_leads_to_list` | write, async | Static-list membership add |
| `remove_leads_from_list` | write, async | Static-list membership removal |
| `list_campaigns` | read, async | Lead API campaign discovery |
| `trigger_campaign` | side-effecting write, async | Request Campaign for supplied leads |
| `list_activity_types` | read, async | Instance activity-type catalog |
| `get_activities` | read, async | Activity history after a supplied instant |
| `get_api_usage` | diagnostic read, async | Current-day API usage |
| `request` | read or write, async | Generic authenticated request (any vendor method and relative path) |
| `describe_companies`, `describe_opportunities`, `describe_opportunity_roles`, `describe_sales_persons` | read, async | Object field list, id, dedupe and searchable fields |
| `get_companies`, `get_opportunities`, `get_opportunity_roles`, `get_sales_persons` | read, async | Record query with internal pagination (roles also by compound key) |
| `sync_companies`, `sync_opportunities`, `sync_opportunity_roles`, `sync_sales_persons` | write, async | createOrUpdate / createOnly / updateOnly |
| `delete_companies`, `delete_opportunities`, `delete_opportunity_roles`, `delete_sales_persons` | destructive write, async | Delete by dedupe fields or id field |
| `describe_named_accounts`, `get_named_accounts`, `get_named_account_lists`, `get_named_account_list_members` | read, async | Named account schema, records, lists and list membership, paged internally |
| `sync_named_accounts`, `sync_named_account_lists` | write, async | createOrUpdate / createOnly / updateOnly (lists: createOnly / updateOnly) |
| `delete_named_accounts`, `delete_named_account_lists` | destructive write, async | Delete by `name` or `marketoGUID` |
| `add_named_account_list_members`, `remove_named_account_list_members` | write, async | Named account list membership by `marketoGUID` |
| `list_custom_object_types`, `describe_custom_objects`, `get_custom_objects` | read, async | Custom object type discovery, per-type schema, records (filter or compound key, paged internally) |
| `sync_custom_objects`, `delete_custom_objects` | write / destructive write, async | Custom object records of one `api_name` type |
| `get_asset_folder`, `get_asset_folder_by_name`, `list_asset_folders`, `get_asset_folder_content` | read, async | Folder/program metadata and folder contents; browse/content page internally |
| `create_asset_folder`, `update_asset_folder`, `delete_asset_folder` | write / destructive write, async | Folder create, name/description correction or clear, archive/unarchive and delete |
| `get_asset_tokens`, `set_asset_token`, `delete_asset_token` | read / write / destructive write, async | Local My Token read, create/upsert/correct/clear and delete on Folder or Program |
| `check_marketo_job_status` | diagnostic read | Polls the platform job ledger for every async verb |

Every verb above except `check_marketo_job_status` is async: it dispatches
`{job_id, status}` and the direct caller polls `check_marketo_job_status`;
no verb calls Marketo on the dispatch path. Finished reads are workspace TSV
handles; finished lead and Lead Database batch writes carry Marketo's
per-record results and tallies; `merge_leads` and `trigger_campaign` finish
with `success` and `request_id`; `test_connection` and `check_setup` finish
with their diagnostic report.
Asset writes instead retain the vendor success/result/requestId/warnings/errors
envelope without inventing per-record statuses or tallies. The
Bulk verbs now submit one platform job per vendor operation. Platform completion
is separate from vendor job completion; batchId/exportId, vendor status and
row reports remain in historical job results. See `03_bulk_and_files.md`.

## Posture and guard sites found in this checkout

Vendor RBAC is the boundary; no plugin-wide read-only posture exists. The
following are the complete relevant posture sites found by the repository
search, refreshed for build 5:

| Site | Evidence and implementation meaning |
|---|---|
| `plugins/marketo_plugin/knowledge_base/hydration_guidance.md:11` | Hydration explicitly says the connector can read and act, including permanent lead delete and irreversible merge. This is not a read-only fence. |
| `plugins/marketo_plugin/knowledge_base/hydration_guidance.md:81` | `check_setup` is intentionally six safe read probes; write/execute permissions (now including every Lead Database object `sync_`/`delete_` verb and the named account list membership writes) are listed as `writes_unverified`. The Asset surface is stated as reachable now through `request`, gated by the Role's Read-Only Assets / Read-Write Assets permissions, which the operator verifies. This limits the diagnostic probe, not the connector's capability. |
| `plugins/marketo_plugin/knowledge_base/01_marketo_overview.md:9` | Overview records full CRUD including delete and merge. |
| `plugins/marketo_plugin/knowledge_base/01_marketo_overview.md:102` | Lead Database object verbs (build 2 objects, named accounts, named account lists and membership, custom objects): writes and deletes first-class, no plugin-side confirmation or read-only gate. |
| `plugins/marketo_plugin/knowledge_base/01_marketo_overview.md:338` | `check_setup` is partial by design; object and Assets permissions are separate operator checks. It does not imply the plugin is read-only. |
| `plugins/marketo_plugin/knowledge_base/01_marketo_overview.md:493` | The only write-side field guard found: `create_or_update_leads` reads live `rest.readOnly` metadata and refuses intended non-key read-only fields before mutation. This is a vendor-field correctness guard, not a global write ban. |
| `plugins/marketo_plugin/knowledge_base/01_marketo_overview.md:628` | Typed Files, Bulk Import and Bulk Extract flows now use the job ledger; the generic request also accepts multipart file parts. Vendor completion stays separate from platform completion. |
| `plugins/marketo_plugin/knowledge_base/02_campaign_flow_inspection_limits.md:21-67` | Campaign flow steps have no published REST read endpoint; smart-list rules require Read-Only Assets and are reachable through `request`. This is a vendor boundary for flow inspection, not a reason to mark the campaign family unsupported. |
| `plugins/marketo_plugin/src/marketo_plugin/constants.py:268` | `CHECK_SETUP_PROBES` enumerates safe reads and `CHECK_SETUP_UNVERIFIED_WRITE_VERBS` deliberately avoids performing writes to test permission. |
| `plugins/marketo_plugin/src/marketo_plugin/plugin.py` (`check_setup` declaration) | The `check_setup` process description repeats the partial probe contract and names the unverified write verbs. |
| `plugins/marketo_plugin/src/marketo_plugin/marketing_actions.py:733` | Runtime implementation calls only side-effect-free probes from `check_setup` and deliberately does not probe write/execute permissions. |
| `plugins/marketo_plugin/src/marketo_plugin/marketing_actions.py:464-551,610-640,691-720` | Existing typed write paths include lead create/update/delete/merge, campaign trigger, and list membership add/remove, each split into a dispatch-path `prepare_` (validation) and a worker-side `execute_` (the vendor call). The list removal path is a verified DELETE-with-body call, not a read-only shim. |
| `plugins/marketo_plugin/src/marketo_plugin/asset_api.py` | All ten Folder/My Token endpoints, form writes, read paging/continuation, envelopes and TSV exports. Vendor RBAC and workspace access remain the boundary; no write-confirmation prompt. |
| `plugins/marketo_plugin/src/marketo_plugin/lead_database_objects.py` | Object `sync_`/`delete_` and named account list membership `add_`/`remove_` verbs post straight to the vendor; validation mirrors the vendor's own enums (named account lists define only createOnly/updateOnly) and 300-entry limits, with no permission prompt or read-only gate. Caller-supplied `api_name`/`list_id` are percent-encoded into one path segment and `.`/`..` are refused, so a caller cannot address a different endpoint; this keeps the foreign-target invariant and is not a capability limit. |
| `plugins/marketo_plugin/src/marketo_plugin/http_client.py:137` | The shared client has GET, POST, and DELETE JSON request methods, plus the generic `request` path. No plugin-side method allowlist restricting requests to reads was found. |
| `plugins/marketo_plugin/src/marketo_plugin/file_api.py:44` | Typed Files inputs and all five published paths; multipart bytes follow vendor Assets permissions. |
| `plugins/marketo_plugin/src/marketo_plugin/bulk_api.py:121` | Typed import/extract lifecycles preserve vendor ids, status, row reports and continuation; no extra capability gate. |
| `plugins/marketo_plugin/src/marketo_plugin/request.py:55` | Generic multipart validation and the registered-origin credential boundary; responses are returned as received and the audit line omits query values, headers and bodies. |
| `plugins/marketo_plugin/src/marketo_plugin/transfer_processes.py:32` | All 35 new operations dispatch through the existing job worker; no vendor calls on the action-dispatch path. |
| `ananta/src/ananta/file_locations/containment.py` | Export paths use saved path addresses, the session folder and solet home, with the installation tree excluded. This is file-containment protection for async reads, not vendor API read-only enforcement. |
| Main-owned full-plugin roster (outside this plugin) | The roster update remains a documentation follow-up owned by Main after delivery. Vendor RBAC remains the connector capability boundary; the roster creates no additional operation restriction. |

The required `rg` search found no Marketo statement allowlist, method allowlist,
or other global read-only gate in `plugins/marketo_plugin` or the Ananta
knowledge bases. The typed lead field guard and export containment gate above
are the relevant narrower guards.

## Proposed build order

Writes and deletes remain first-class in every unit. Each typed unit must
inventory create/read/update-or-correct/clear-or-retire/delete where the vendor
offers those transitions, name authorization and stale-write premises, and
preserve current and historical/job readback. Exact endpoint shapes marked
“to verify” in the surface table are a prerequisite to implementation.

| Order | Unit | Coverage | Size | Notes |
|---:|---|---|---|---|
| 1 | Lead Database writes | Companies, opportunities (with roles), sales persons: done in build 2 (unit `unt_ceaa4eb3-cf88-4cce-b521-08b3b8202d67`). Named accounts, named account lists with membership, and custom object records: done in build 3 (unit `unt_84914081-a7b4-418a-9a4a-bc702b7259a8`). Remaining for build 4: custom object type schema verbs, named account field-schema browse, and lead lifecycle corrections and clear/retire paths | L | Typed lifecycle conventions established by build 2 and reused by build 3; retain current lead verbs. |
| 2 | Lists and membership | Smart/static list objects, smart-list rules, membership create/remove/clear, list delete where exposed (next, build 4, with the Asset API families in order 7-9) | M | Reuse current list membership patterns; verify the Read-Only Assets / Read-Write Assets permissions and exact paths. |
| 3 | Generic authenticated request | One method/path/query/body/file verb for the long tail with target invariant, RBAC errors, and bounded async/job mode | M | Done (build 1) as `request` in unit `unt_85e85cee-1e7d-4b39-b873-fd89be4086c1` (JSON/form bodies; multipart added in build 5). |
| 4 | Bulk Import: leads | Done build 5: multipart submit (auto-enqueue), status, failures and warnings through job ledger | L | Hermetic job-ledger transcript; no live tenant mutation used as a test. |
| 5 | Bulk Extract: activities | Done build 5: list/create/enqueue/status/download/cancel through job ledger | L | Hermetic job-ledger transcript; caller preserves exportId across operations. |
| 6 | Bulk Extract/Import: program members and custom objects | Program-member extracts and program-member/custom-object imports done build 5; custom-object extracts remain build 6 | L | No published bulk activity-import endpoint; no invented cancel/delete for imports. |
| 7 | Programs and smart campaigns | Asset CRUD, clone, approve, archive/delete, metadata/rules; preserve trigger side-effect warnings | L | Do not claim flow-step inspection where the vendor does not serve it. |
| 8 | Emails, templates, landing pages, forms | Asset CRUD, clone, approve, archive/delete | L | Verify content/file payload and Read-Only Assets/Read-Write Assets permissions. |
| 9 | Files, tokens, folders, tags, channels | Folders and My Tokens done in build 4 (all ten endpoints); files done in build 5 (five published endpoints); tags/channels and assignment lifecycle remain | L | Multipart support shipped; Files has no published delete/archive operation. |
| 10 | Identity and cross-family completion | Any remaining identity read/introspection/revocation requested by the vendor surface; acceptance and lifecycle audit across all families | S/M | `test_connection` already covers token mint; do not add speculative token operations. |

Rough sizes are per future unit, not a score or a permission decision. A unit
may split further if endpoint verification shows materially different job or
payload contracts, but every split must retain a generic authenticated route and
must not leave a family without a verb or reachable generic path.

## Acceptance mapping

* **“Each Marketo API family above has typed verbs or is reachable through the
  generic request verb.”** The surface inventory names every family from the
  ticket, maps current typed verbs, and names the generic `request` verb
  (shipped in build 1) as the route for every family still marked gap.
* **“A bulk lead import runs end to end through the job ledger.”** Build order
  unit 4 implements upload/create/enqueue/poll/terminal row results and adds an
  end-to-end job-ledger test using the registered credential's vendor RBAC.
* **“A bulk activity extract runs end to end through the job ledger.”** Build
  order unit 5 implements the corresponding extract job and download lifecycle,
  with terminal/error evidence retained by the job ledger and an end-to-end
  test.
* **“A vendor-API-coverage table lists every family and nothing is marked
  unsupported.”** The Surface inventory table includes Lead Database objects,
  Lists, Programs, Smart Campaigns, Smart-List rules, every named Asset type,
  Activities/types, Bulk Extract/Import object families, Identity, and the
  generic request route. It uses “gap” only for current implementation status;
  no family is marked unsupported.

The pass condition for this file is that subsequent build units can use the
table as their specification, the plugin knowledge-base search can retrieve it,
and the document/KB gates pass. The table is not evidence that future verbs,
bulk jobs, or generic request behavior already exist.

## Build 4 verification and lifecycle premises

Endpoint verification (2026-11-07): Adobe's [Asset endpoint reference](https://developer.adobe.com/marketo-apis/api/asset),
backed by [Adobe's published swagger-asset.json](https://github.com/AdobeDocs/marketo-apis/blob/main/static/swagger-asset.json),
and the [Folders](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/assets/folders)
and [Tokens](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/assets/tokens) guides.
The reference's `workSpace` spelling is used on folder queries. Folder writes
and token writes use form encoding; folder parent/root objects are embedded JSON.

Folders support create/read, name and description correction, empty-description
clear, archive/unarchive and terminal delete of empty non-system folders.
Programs may be returned by folder reads, but their mutations belong to Programs.
My Tokens support create/read/upsert/correction, empty-value clear, delete and
recreation from a saved value. No separate close/reopen or approval operation is
published for tokens. Folder unarchive is the supported reversal of archive;
a deleted folder cannot be reopened with its old id. No vendor move endpoint
is advertised by these wrappers.

These endpoints offer no expected-version, ETag or conditional write parameter:
writes are last-write-wins. Serialize changes and read back to detect competing
edits; stale-write refusal cannot be promised without vendor support. Current
state is read through the typed reads; platform job handles retain each call's
result/error for historical observation, without inventing vendor revision history.
Read/write authorization is the registered API user's Assets permissions plus
workspace access. `check_setup` never performs these mutations to probe access.

Remaining typed families: Smart Lists together with their current rule mutation
surface, Programs, Smart Campaigns, email/templates, landing pages, forms,
tags/channels, schema metadata and custom-object Bulk Extract (build 6). Their
interim route remains `request`, including multipart upload support. The three build-2
follow-ups were already present at this build's base: the Assets permission
label, opportunity-role compound paging, and updateOnly sync regression test.

## Build 5 verification and posture sites

Files endpoints and multipart rules were verified on 2026-11-07 against
[Adobe Files](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/assets/files)
and [Asset OpenAPI](https://github.com/AdobeDocs/marketo-apis/blob/main/static/swagger-asset.json).
The complete published Files section contains five operations and no delete,
archive or reopen endpoint. File content replacement takes id in the path,
with the file part in the body, as the guide demonstrates.

Bulk paths and parameters were verified against
[Lead Database OpenAPI](https://github.com/AdobeDocs/marketo-apis/blob/main/static/swagger-mapi.json)
and Adobe's [lead import](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/bulk-import/bulk-lead-import),
[custom-object import](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/bulk-import/bulk-custom-object-import),
[program-member import](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/bulk-import/bulk-program-member-import),
[lead extract](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/bulk-extract/bulk-lead-extract),
[activity extract](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/bulk-extract/bulk-activity-extract),
and [program-member extract](https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/bulk-extract/bulk-program-member-extract)
guides. The current Activity guide makes fields optional, despite the older
OpenAPI required list. Nested filters retain all documented options, including
primaryAttributeValues/primaryAttributeValueIds and programIds/nurtureCadence/
statusNames/isExhausted/updatedAt. Imports auto-enqueue, and publish only
submission/status/failures/warnings; no cancel/delete or bulk activity import
endpoint appears in this reference. Correct by another import; cancel an
extract and create a replacement rather than inventing correction/reopen.

New posture sites: `multipart.py` (part integrity), `file_api.py` (all Files
paths and parameters), `bulk_api.py` (bulk lifecycle and envelopes),
`transfer_metadata.py` / `transfer_processes.py` (35 caller contracts),
`request.py` / `http_client.py` (same-origin multipart; responses as received,
audit lines without query values, headers or bodies), `completion_templates.py`
(platform versus vendor completion), `constants.py` (writes remain unverified
by check_setup), and this article plus overview/hydration/process KB companions.
No new dependency, role restriction or confirmation prompt is introduced.
The full-plugin roster remains a Unit follow-up owned by the landing queue.
