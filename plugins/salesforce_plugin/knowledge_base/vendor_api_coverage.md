# Salesforce Vendor API Coverage (`salesforce_plugin`)

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:salesforce, domain:local-solet, domain:cloud-solet

Embedding Description: Salesforce vendor API coverage inventory — the complete requested API-family surface, current salesforce_plugin verbs including the composite, tooling, bulk_jobs, reports, dashboards, rest_resources, actions, metadata and content_files families, remaining typed gaps and their current routes, why SOAP-only calls and streaming subscription need a later build, read/write posture evidence, build order, and acceptance mapping.

This is the specification and progress record for the build units under
Feature Request `frq_770ad1d9fada49078714e83aa4fee0d3`. It inventories the API
families named by that ticket against the current checkout. Build 1 added the
generic `authenticated_request` verb; build 3 (2026-11-07) added the typed
`composite`, `tooling` and `bulk_jobs` families, Bulk ingest `hardDelete` and
the `include_deleted` (queryAll) selector; build 4 (2026-11-07) added the
typed `reports`, `dashboards`, `rest_resources` and `actions` families and the
Tooling `sobject_info` operation; build 5 (2026-11-07) added the typed
`metadata` family (Metadata API deployments over the REST deployRequest
resource) and `content_files` family (ContentVersion upload, verified download
and ContentDocument delete), Tooling composite and apexCompileResults, Bulk
API 2.0 parallel result pages, report instance delete and sObject
relationship update and delete. A family without a typed verb is still in
scope; its current route is named in its row. No family is excluded from the
inventory, and no family is read-only: Salesforce's permissions on the
registered user are the only boundary.

Two limits of the transport shape what is left. Every verb runs through the
operator's `sf` CLI and the plugin never holds an access token
(`client.py:1-17`). The REST, Tooling, Bulk, Analytics and Metadata REST
resources take the CLI's `Authorization: Bearer` header, so the generic
`authenticated_request` and the typed verbs reach them. The SOAP API and the
SOAP-only Metadata calls take the token inside the SOAP `SessionHeader`
instead, which the plugin cannot write, so an XML body sent through
`authenticated_request` does not authenticate there; build 4's statement
that it reached SOAP was not verified and is corrected here. Their routes are
Apex `Database` methods through `tooling` `execute_anonymous` today, and the
`sf` CLI's own Metadata commands in build 6. Platform Event and Change Data
Capture subscription needs a streaming session (CometD cookies or the Pub/Sub
API over gRPC), which the one-request-per-call CLI transport is not.

## Coverage table

The base paths below are the Salesforce paths, relative to the registered org
host. `{version}` is the configured API version (currently `62.0`). Every typed
operation's method and path is listed in its process description under
`knowledge_base/processes/` and in `plugins/salesforce_plugin/src/salesforce_plugin/typed_families.py`.

| API family | Protocol and base path | Vendor capability and data direction | Current plugin coverage | Gap and next verb |
|---|---|---|---|---|
| REST API: sObject CRUD | HTTPS REST, `/services/data/v{version}/sobjects/` and `/sobjects/{Object}/{Id}` | Create, read, update, and delete records; reads, writes, and deletes | `create_record` (write), `get_record` (read), `update_record` (write), and `delete_record` (delete). `rest_resources` adds records by external id (`get_by_external_id`, `upsert_by_external_id` with query `updateOnly`, `delete_by_external_id`), `relationship` traversal, and the `updated` and `deleted` change reads; `rest_resources` `relationship_update` (PATCH) and `relationship_delete` (DELETE) write through a relationship field | Covered (builds 4 and 5). Undelete has no REST resource: run Apex `Database.undelete` through `tooling` `execute_anonymous` (see the SOAP API row) |
| REST API: composite | HTTPS REST, `/services/data/v{version}/composite`, `/composite/batch`, `/composite/tree/{Object}`, `/composite/sobjects` | Bundle subrequests with reference IDs, batches, record trees and up-to-200-record Collections; reads, writes, and deletes | `composite` verb: `composite`, `batch`, `tree`, `collections_create`, `collections_retrieve` (GET), `collections_retrieve_many` (POST), `collections_update`, `collections_upsert`, `collections_delete`; per-part failures counted in `failures` | Covered (build 3) |
| REST API: composite graph | HTTPS REST, `/services/data/v{version}/composite/graph` | Dependency-aware graphs of subrequests, each graph all-or-nothing; reads, writes, and deletes | `composite` verb, operation `graph`; failed graphs counted in `failures` | Covered (build 3) |
| REST API: query and queryAll | HTTPS REST, `/services/data/v{version}/query` and `/queryAll` | SOQL reads; `queryAll` includes deleted (recycle bin) and archived records | `soql_query` and `export_soql`; `include_deleted=true` selects queryAll (`sf data query --all-rows`), results written to a contained TSV. `rest_resources` `query`, `query_all` and `query_more` page the vendor response inline by `nextRecordsUrl`; `explain` returns query plans | Covered (build 4) |
| REST API: describe and org resources | HTTPS REST, `/services/data/v{version}/sobjects/{Object}/describe`, global `/sobjects/`, `/sobjects/{Object}`, `/limits`, `/recent`, `/search`, `/parameterizedSearch` | Object and field metadata, org limits, recently viewed items and search; reads | `describe_sobject` and `list_sobjects` (trimmed shapes); `rest_resources` `resources`, `limits`, `recent`, `describe_global`, `sobject_info`, `describe` (full metadata, `If-Modified-Since` passes through), `search` and `parameterized_search` | Covered (build 4) |
| Bulk API 2.0: ingest | HTTPS REST, `/services/data/v{version}/jobs/ingest` | Asynchronous insert, update, upsert, delete, and hard delete of CSV records; writes and deletes | `bulk_ingest_submit` (insert, update, upsert, delete, hardDelete via `sf data delete bulk --hard-delete`), `bulk_job_status`, `bulk_job_results`, `bulk_job_abort`; `bulk_jobs` operations `ingest_list` and `ingest_delete` | Covered (build 3). hardDelete needs the vendor's Bulk API Hard Delete permission; the plugin adds no gate of its own |
| Bulk API 2.0: query | HTTPS REST, `/services/data/v{version}/jobs/query` | Asynchronous SOQL query (`query` or `queryAll`) and paged CSV result retrieval; reads, abort and delete | `bulk_jobs` verb: `query_create`, `query_get`, `query_list` (paged by `queryLocator`), `query_results` (one CSV page per call under a saved location, paged by `Sforce-Locator`, `maxRecords`), `query_abort`, `query_delete`; `query_result_pages` (GET `/jobs/query/{id}/resultPages`, API 58.0 and later) returns up to five page locators per call for parallel `query_results` downloads in `pagination.result_locators`; `query_result_pages_more` follows the response's `nextRecordsUrl` verbatim | Covered (builds 3 and 5) |
| Tooling API | HTTPS REST, `/services/data/v{version}/tooling/` | Development metadata and Tooling objects, queries, search, test runs, completions, and record writes and deletes | `tooling` verb: `query`, `query_more` (follows `nextRecordsUrl`), `search`, `run_tests_async`, `run_tests_sync`, `completions`, `list_sobjects`, `sobject_info`, `describe`, `create`, `get`, `update`, `delete`, `composite_resources` and `composite` (`/tooling/composite`, up to 25 subrequests, failed parts counted in `failures`), `apex_compile_results` (API 68.0 and later, so the plugin `api_version` must be raised from 62.0; body exactly `{}`; `PARTIAL_FAILURE` results counted in `failures`) | Covered (build 3; `sobject_info` build 4; composite and apexCompileResults build 5). Not yet typed, reachable through `authenticated_request`: `tests` (API 65.0), `symbols` (API 68.0, beta) and the ApexLog `Body` text, which are later than the pinned API version 62.0 or plain text |
| SOAP API | HTTPS SOAP, `/services/Soap/u/{version}` (enterprise/user) or `/services/Soap/c/{version}` (partner/client) | Session-based enterprise/partner object operations; reads, writes, and deletes | No SOAP call authenticates through the CLI transport: the SOAP API takes the access token in the `SessionHeader`, which the plugin never holds. The operations with no REST equivalent run today as Apex through `tooling` `execute_anonymous`: `Database.merge`, `Database.convertLead`, `Database.undelete` and `Database.emptyRecycleBin` | Build 6: typed `merge`, `convertLead`, `undelete` and `emptyRecycleBin` over a route that authenticates (generated Apex through `execute_anonymous`, or a CLI command), never by moving the token into this process |
| Metadata API | HTTPS REST, `/services/data/v{version}/metadata/deployRequest` (deploy); HTTPS SOAP, `/services/Soap/m/{version}` (every call, and the only route for retrieve, describeMetadata, listMetadata and readMetadata) | `deploy`, `retrieve`, and `describeMetadata`; asynchronous metadata writes/reads, deletes through destructiveChanges | `metadata` verb over the REST resources the Metadata API guide documents: `deploy` (POST `/metadata/deployRequest`, multipart `json` deployOptions plus the `.zip` package read under a saved location; checkOnly validation, test levels and destructiveChanges deletes), `deploy_status` (`includeDetails`), `deploy_cancel` (PATCH status Canceling) and `deploy_quick` (POST a validated deployment); failed components and tests counted in `failures` | Deploy covered (build 5). retrieve, describeMetadata, listMetadata and readMetadata are SOAP-only (the guide states retrieval always uses SOAP); build 6 types them over the `sf` CLI's `project retrieve start`, `org list metadata-types` and `org list metadata` commands with retrieved zips written under a saved location |
| Apex REST | HTTPS REST, user-defined `/services/apexrest/{namespace}/{resource}` | Custom Apex HTTP resources; direction is resource-defined and can read, write, or delete | `authenticated_request` with method/body/response mode | Org-defined resources have no fixed vendor shape; the generic verb is the intended route. Invocable Apex is typed through `actions` |
| Invocable Actions | HTTPS REST, `/services/data/v{version}/actions/standard` and `/actions/custom/{type}` | Discover and invoke standard actions, invocable Apex, autolaunched flows, quick actions and email alerts; invocation can write | `actions` verb: `list`, `list_standard`, `list_custom`, `list_custom_type`, `describe_standard`, `describe_custom`, `invoke_standard`, `invoke_custom`; results whose `isSuccess` is false are counted in `failures` | Covered (build 4) |
| Anonymous Apex | HTTPS REST Tooling action, `/services/data/v{version}/tooling/executeAnonymous` | Execute one-off Apex; write-capable side effects and diagnostic reads | `tooling` verb, operation `execute_anonymous` (`anonymousBody`); a compile or run failure is counted in `failures` with `compileProblem` and `exceptionMessage` in the body | Covered (build 3) |
| Reports | HTTPS REST, `/services/data/v{version}/analytics/reports/` and `/analytics/reportTypes` | List, create, clone, describe, run, save and delete reports; report types; reads, writes, and deletes | `reports` verb: `list`, `create`, `clone`, `describe`, `run`, `run_filtered`, `run_async`, `instances`, `instance_results`, `query`, `update`, `delete`, `report_types`, `describe_report_type`; a run with `allData` false returns `pagination.report_all_data`; `instance_delete` (DELETE an instance whose status is Success or Error) | Covered (builds 4 and 5) |
| Dashboards | HTTPS REST, `/services/data/v{version}/analytics/dashboards/` | List, describe, refresh, clone, save, delete and obtain dashboard data; reads, writes, and deletes | `dashboards` verb: `list`, `clone`, `describe` (query `loadComponentProperties`), `results`, `refresh` and `status` (query `runningUser`, `filter1`, `filter2`, `filter3`), `update` (query `isStickyFilterSave`), `delete` | Covered (build 4). Not yet typed, reachable through `authenticated_request`: the dashboard components POST, filter options analysis and filter operators |
| Platform Events | REST publish at `/services/data/v{version}/sobjects/{EventApiName}__e`; subscription over CometD or the Pub/Sub API | Publish is a write; subscription is a streaming read | Publish: `create_record` with an `__e` sObject publishes one event, and `composite` `collections_create` publishes up to 200; `rest_resources` `describe` reads an event's or change event's fields | Publish and describe covered. Build 6: `platform_event_subscribe` (also Change Data Capture channels) needs a streaming-capable transport with replay and durable cursor evidence; the CLI transport makes one request per call and holds no CometD session cookie, and the Pub/Sub API needs a gRPC dependency that is not declared |
| Salesforce Files | HTTPS REST, `/services/data/v{version}/sobjects/ContentVersion`, `/sobjects/ContentVersion/{Id}/VersionData`, `/sobjects/ContentDocument/{Id}` | Upload a file or a new version, download a version's bytes, delete a document and all its versions; reads, writes and deletes | `content_files` verb: `upload` (non-multipart base64 `VersionData` from a file under a saved location; the vendor limits this form to 37.5 MB of base64 data), `download` (the version's `ContentSize` and `Checksum` read first, then `--stream-to-file` under a saved location; the bytes are kept only when size and MD5 match), `delete_document` | Covered (build 5). Build 6: multipart upload for files over the non-multipart limit (up to 2 GB for ContentVersion) |

## Current plugin verb map

This is the measured mapping from the process KB and source. All data/org verbs
use the plugin's deferred job envelope; the Salesforce result is a later
completion, not the initial queue receipt.

| Current verb | Direction | Current family coverage |
|---|---|---|
| `soql_query` | read (writes a contained TSV) | REST query and queryAll (`include_deleted`) |
| `export_soql` | read (writes a contained TSV) | REST query and queryAll, larger export |
| `get_record` | read | REST sObject record GET |
| `describe_sobject` | read | REST sObject describe |
| `list_sobjects` | read | REST global describe |
| `create_record` | write | REST sObject create; Platform Event publish for an `__e` type |
| `update_record` | write | REST sObject update |
| `delete_record` | delete | REST sObject delete |
| `composite` | read/write/delete | REST composite, composite graph, batch, sObject tree, sObject Collections |
| `tooling` | read/write/delete/execute | Tooling API and anonymous Apex |
| `bulk_ingest_submit` | write/delete | Bulk API 2.0 ingest insert/update/upsert/delete/hardDelete |
| `bulk_job_status` | read | Bulk API 2.0 ingest job status |
| `bulk_job_results` | read (writes contained CSVs) | Bulk API 2.0 ingest result retrieval |
| `bulk_job_abort` | write/control | Bulk API 2.0 ingest abort |
| `bulk_jobs` | read/write/delete (query results to contained CSVs) | Bulk API 2.0 query job lifecycle; ingest job list and delete |
| `reports` | read/write/delete | Reports REST API: runs, instances, clone, save, delete, report types |
| `dashboards` | read/write/delete | Dashboards REST API: results, refresh, status, clone, save, delete |
| `rest_resources` | read/write/delete | REST org resources, describe, external-id upsert and delete, relationships, updated/deleted, paged query and queryAll, explain, search |
| `actions` | read/execute | Invocable Actions discovery and invocation |
| `metadata` | write/delete/control (reads a contained `.zip`) | Metadata API REST deployRequest: deploy, status, cancel, quick deploy |
| `content_files` | read/write/delete (contained local files) | ContentVersion upload and verified download, ContentDocument delete |
| `authenticated_request` | any | Every other relative REST endpoint: Apex REST and unwrapped REST, Tooling and Analytics resources (not SOAP; see the SOAP API row) |
| `test_connection` | diagnostic read | Registered-org binding check |
| `probe_cli` | local diagnostic read | Local `sf` executable only |
| `provision_cli` | local system write | Install/bind the `sf` CLI; not a Salesforce API family |

The typed families call the same transport as `authenticated_request`, so
the registered-org binding and the refusal of caller `Authorization`/`Host`
headers apply to every typed operation unchanged. Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.

A typed operation adds what the generic verb cannot know: the documented method
and path, documented query parameter names and value types, required body
members, an `error_code` for a vendor HTTP failure, a `failures` count for
per-part outcomes inside a 2xx, and pagination hints. These are read from
the vendor response itself. An exception raised while reading vendor data (for example
a malformed `nextRecordsUrl`) is contained and recorded only by type in
`vendor_structure_advisory`; exception text that does leave as an error
message is returned as received, and the worker log records only the
exception type. Because each typed
operation documents its response type, its JSON or Bulk CSV body is separated
from the CLI's printed headers by a proof that does not apply to the generic
verb (`request_actions._typed_boundary`). A Files download is the one call
whose bytes do not pass through the transcript: the CLI's `--stream-to-file`
writes them unchanged and prints no status, so `transfers` proves them against
the version's own `ContentSize` and `Checksum` before keeping them, and the
audit line for that call is written by `request_actions.stream_to_file`.

## Read/write posture evidence

These are the posture sites that follow-on units must update or preserve.
Line references are to this checkout after build 5.

| Site | Evidence |
|---|---|
| `plugins/salesforce_plugin/plugin.yaml:3-12` | Plugin description lists SOQL including queryAll, record CRUD including delete, Bulk ingest and query jobs, composite, Tooling with anonymous Apex, Reports and Dashboards, REST resources with relationship writes, invocable actions, Metadata deployments, Salesforce Files, and the generic request |
| `plugins/salesforce_plugin/plugin.yaml:28-41` | Full R/W including delete under RATIFY-2 and USER ruling `rul_4c99a30d`; hardDelete, Collections deletes, Tooling deletes, anonymous Apex, report, report instance and dashboard deletes, external-id and relationship deletes, action invocation, destructive metadata deployments and ContentDocument deletes are first-class; SOAP-only calls and streaming subscription are a later build, with the reason |
| `plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md:24-58` | Overview posture: vendor permissions are the only boundary, no read-only mode, role request or confirmation prompt; the build 5 families and why SOAP-only calls need a later build |
| `plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md:188-215` | The 25-verb inventory including the nine typed families and the generic request |
| `plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md:239-294` | Bulk ingest with first-class hardDelete and the `bulk_jobs` query lifecycle |
| `plugins/salesforce_plugin/knowledge_base/hydration_guidance.md:13` | Hydration presents creates, updates, permanent deletes, hardDelete, report and dashboard changes, invocable actions, Tooling changes, anonymous Apex, metadata deployments and Files uploads and deletes as the operator's own user, and states there is no read-only integration user |
| `plugins/salesforce_plugin/knowledge_base/hydration_guidance.md:26` | Setup covers Bulk ingest (import and export roots), Bulk query exports (export roots only), deploy packages and file uploads (import roots) and file downloads (export roots) |
| `plugins/salesforce_plugin/src/salesforce_plugin/plugin.py:19-32` | Source-level verb posture, with no plugin-wide read-only gate |
| `plugins/salesforce_plugin/src/salesforce_plugin/typed_families.py:44-45` | Typed families classify nothing as read or write; Salesforce permissions decide every operation |
| `plugins/salesforce_plugin/src/salesforce_plugin/transfers.py:23` | The local-file operations (deploy, upload, download) classify nothing as read or write either |
| `plugins/salesforce_plugin/src/salesforce_plugin/constants.py:100-101` | The Bulk ingest operation set includes `hardDelete` |
| `plugins/salesforce_plugin/src/salesforce_plugin/bulk_actions.py:69-78` | `hardDelete` maps to `sf data delete bulk --hard-delete`; no local refusal |
| `plugins/salesforce_plugin/tests/smoke_bulk.py:211` | Smoke coverage proves the hardDelete command line and that unknown operations are still refused |
| `ananta/knowledge_bases/ananta_platform/23_plugin_inventory/01_full_plugin_roster.md:120` | Roster records `salesforce_plugin` as active and full R/W including delete; its "`run_apex`/bulk ops/file verbs out of v1" note is stale after builds 3, 4 and 5 and is outside this plugin's scope fence (recorded as a follow-up on the build 5 Unit) |

There is no Salesforce plugin statement allowlist, method allowlist, or
read-only integration-user guard in the source. The remaining local limits are
target-org pinning, the refusal of caller credential/host headers, input shape
checks that mirror the vendor documents, and path containment for local
import/export files, plus the size and checksum proof of a downloaded file. Those are integrity boundaries, not reasons to mark an API
family unsupported.

## Build order

Writes and deletes stay first-class and retain the existing job ledger/audit
shape. Sizes are rough unit estimates, not a rescore of the registered plan.

| Order | Unit | Deliverable | Status |
|---:|---|---|:---:|
| 1 | Generic authenticated request | `authenticated_request` forwards method/path/query/headers and raw JSON/XML/text body through the pinned-org CLI, with deferred completion and audit evidence built without query values, headers or body (`unt_12041887-9b46-485d-a4e4-1d89e82c7d3a`) | Built |
| 2 | REST composite and queryAll | `composite` family and `include_deleted` on the SOQL verbs | Built (build 3) |
| 3 | Bulk API 2.0 completion | hardDelete, `bulk_jobs` query lifecycle, ingest list and delete | Built (build 3) |
| 4 | Tooling API and anonymous Apex | `tooling` family including `execute_anonymous` | Built (build 3) |
| 5 | Reports and Dashboards | Typed `reports` and `dashboards` families | Built (build 4) |
| 6 | REST remainder | `rest_resources`: relationship traversal, updated/deleted change reads, external-id get/upsert/delete, paged query and queryAll, query explain, limits, recent items, object info, search; Tooling `sobject_info` | Built (build 4) |
| 7 | Invocable actions | `actions`: standard and custom action discovery, describe and invoke | Built (build 4) |
| 8 | Metadata API | Typed `metadata` family with contained zip packages: deploy, status, cancel and quick deploy over the REST deployRequest resource | Built (build 5); SOAP-only retrieve, describeMetadata, listMetadata and readMetadata move to build 6 (row 12) |
| 9 | SOAP API | Partner SOAP operations with no REST equivalent (`merge`, `convertLead`, `undelete`, `emptyRecycleBin`) | Build 6: the SOAP SessionHeader needs the access token the plugin never holds; reachable now as Apex `Database` methods through `tooling` `execute_anonymous` |
| 10 | Platform Event subscription | Streaming-capable subscribe with replay/cursor evidence, including Change Data Capture channels | Build 6: needs a streaming transport (CometD session or Pub/Sub gRPC) |
| 11 | REST and Tooling remainder | Tooling composite and apexCompileResults, Bulk parallel result pages, report instance delete, relationship update and delete, ContentVersion upload, verified download and ContentDocument delete (`content_files`) | Built (build 5) |
| 12 | Build 6 | Metadata retrieve, describeMetadata, listMetadata and readMetadata over the `sf` CLI's Metadata commands; typed Partner operations (row 9); Platform Event and Change Data Capture subscription (row 10); multipart ContentVersion upload over the non-multipart limit; dashboard components POST, filter options analysis and filter operators; the GET form of parameterizedSearch; Tooling `tests`, `symbols` and ApexLog `Body` | Not started |

For every write or delete unit, include authorization, optimistic-concurrency
premises where the vendor supports them (`If-Match`/`If-Unmodified-Since` pass
through the `headers` parameter), append-only job/audit history, current and
historical result readback, stale-write refusal (`sf.precondition_failed`),
reversal or abort semantics where available, and terminal completion behavior.

## Acceptance mapping

| Ticket acceptance clause | This inventory's implementation mapping |
|---|---|
| Every vendor API family has typed verbs or the generic request verb | Every named family appears in the coverage table with its typed verb or current route. The SOAP API's Partner operations and Platform Event subscription are the exceptions to "through the generic verb": the SOAP SessionHeader and a streaming session are outside the CLI transport. Partner operations run now as Apex through `tooling` `execute_anonymous`; subscription is build 6. |
| `delete_record` succeeds against a scratch org | `delete_record` is the typed REST sObject delete path; `composite` `collections_delete`, `tooling` `delete` and `rest_resources` `delete_by_external_id` add batch, Tooling and external-id deletes. A scratch-org run needs a registered org and is not part of the hermetic smokes. |
| Bulk delete succeeds against a scratch org | `bulk_ingest_submit(operation=delete)` and `operation=hardDelete` are the typed Bulk v2 delete paths; a scratch-org run needs a registered org. |
| Coverage table exists in the plugin KB | This file is the table and is under `plugins/salesforce_plugin/knowledge_base/`. |
| No family is marked unsupported | Every gap names its current route and a typed follow-up in build 6 (row 12 of the build order). |
| Hydration guidance no longer instructs a read-only integration user | `hydration_guidance.md:13` describes full operator read/write/delete access, including hardDelete and anonymous Apex, and states there is no read-only integration user. |

The vendor RBAC boundary remains authoritative: a typed or generic request may
reach only what the registered Salesforce credential is permitted to reach.
That is distinct from silently omitting an API family from the product
surface.
