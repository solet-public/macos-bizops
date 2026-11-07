# Salesforce Vendor API Coverage (`salesforce_plugin`)

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:salesforce, domain:local-solet, domain:cloud-solet

Embedding Description: Salesforce vendor API coverage inventory — the complete requested API-family surface, current salesforce_plugin verbs including the composite, tooling, bulk_jobs, reports, dashboards, rest_resources and actions families, remaining typed gaps routed through the generic authenticated request verb, read/write posture evidence, build order, and acceptance mapping.

This is the specification and progress record for the build units under
Feature Request `frq_770ad1d9fada49078714e83aa4fee0d3`. It inventories the API
families named by that ticket against the current checkout. Build 1 added the
generic `authenticated_request` verb; build 3 (2026-11-07) added the typed
`composite`, `tooling` and `bulk_jobs` families, Bulk ingest `hardDelete` and
the `include_deleted` (queryAll) selector; build 4 (2026-11-07) added the
typed `reports`, `dashboards`, `rest_resources` and `actions` families and the
Tooling `sobject_info` operation. A family without a typed verb is
still in scope and reachable now through `authenticated_request`. No family is
excluded from the inventory, and no family is read-only: Salesforce's
permissions on the registered user are the only boundary.

## Coverage table

The base paths below are the Salesforce paths, relative to the registered org
host. `{version}` is the configured API version (currently `62.0`). Every typed
operation's method and path is listed in its process description under
`knowledge_base/processes/` and in `plugins/salesforce_plugin/src/salesforce_plugin/typed_families.py`.

| API family | Protocol and base path | Vendor capability and data direction | Current plugin coverage | Gap and next verb |
|---|---|---|---|---|
| REST API: sObject CRUD | HTTPS REST, `/services/data/v{version}/sobjects/` and `/sobjects/{Object}/{Id}` | Create, read, update, and delete records; reads, writes, and deletes | `create_record` (write), `get_record` (read), `update_record` (write), and `delete_record` (delete). `rest_resources` adds records by external id (`get_by_external_id`, `upsert_by_external_id` with query `updateOnly`, `delete_by_external_id`), `relationship` traversal, and the `updated` and `deleted` change reads | Covered (build 4). Undelete has no REST resource; it is a Partner SOAP call (build 5) |
| REST API: composite | HTTPS REST, `/services/data/v{version}/composite`, `/composite/batch`, `/composite/tree/{Object}`, `/composite/sobjects` | Bundle subrequests with reference IDs, batches, record trees and up-to-200-record Collections; reads, writes, and deletes | `composite` verb: `composite`, `batch`, `tree`, `collections_create`, `collections_retrieve` (GET), `collections_retrieve_many` (POST), `collections_update`, `collections_upsert`, `collections_delete`; per-part failures counted in `failures` | Covered (build 3) |
| REST API: composite graph | HTTPS REST, `/services/data/v{version}/composite/graph` | Dependency-aware graphs of subrequests, each graph all-or-nothing; reads, writes, and deletes | `composite` verb, operation `graph`; failed graphs counted in `failures` | Covered (build 3) |
| REST API: query and queryAll | HTTPS REST, `/services/data/v{version}/query` and `/queryAll` | SOQL reads; `queryAll` includes deleted (recycle bin) and archived records | `soql_query` and `export_soql`; `include_deleted=true` selects queryAll (`sf data query --all-rows`), results written to a contained TSV. `rest_resources` `query`, `query_all` and `query_more` page the vendor response inline by `nextRecordsUrl`; `explain` returns query plans | Covered (build 4) |
| REST API: describe and org resources | HTTPS REST, `/services/data/v{version}/sobjects/{Object}/describe`, global `/sobjects/`, `/sobjects/{Object}`, `/limits`, `/recent`, `/search`, `/parameterizedSearch` | Object and field metadata, org limits, recently viewed items and search; reads | `describe_sobject` and `list_sobjects` (trimmed shapes); `rest_resources` `resources`, `limits`, `recent`, `describe_global`, `sobject_info`, `describe` (full metadata, `If-Modified-Since` passes through), `search` and `parameterized_search` | Covered (build 4) |
| Bulk API 2.0: ingest | HTTPS REST, `/services/data/v{version}/jobs/ingest` | Asynchronous insert, update, upsert, delete, and hard delete of CSV records; writes and deletes | `bulk_ingest_submit` (insert, update, upsert, delete, hardDelete via `sf data delete bulk --hard-delete`), `bulk_job_status`, `bulk_job_results`, `bulk_job_abort`; `bulk_jobs` operations `ingest_list` and `ingest_delete` | Covered (build 3). hardDelete needs the vendor's Bulk API Hard Delete permission; the plugin adds no gate of its own |
| Bulk API 2.0: query | HTTPS REST, `/services/data/v{version}/jobs/query` | Asynchronous SOQL query (`query` or `queryAll`) and paged CSV result retrieval; reads, abort and delete | `bulk_jobs` verb: `query_create`, `query_get`, `query_list` (paged by `queryLocator`), `query_results` (one CSV page per call under `export_allowed_roots`, paged by `Sforce-Locator`, `maxRecords`), `query_abort`, `query_delete` | Covered (build 3). Build 5: the parallel result-pages resource once its path is verified against the current Bulk API 2.0 guide |
| Tooling API | HTTPS REST, `/services/data/v{version}/tooling/` | Development metadata and Tooling objects, queries, search, test runs, completions, and record writes and deletes | `tooling` verb: `query`, `query_more` (follows `nextRecordsUrl`), `search`, `run_tests_async`, `run_tests_sync`, `completions`, `list_sobjects`, `sobject_info`, `describe`, `create`, `get`, `update`, `delete` | Covered (build 3; `sobject_info` build 4). Build 5: Tooling composite resources; `authenticated_request` reaches `/tooling/composite` now |
| SOAP API | HTTPS SOAP, `/services/Soap/u/{version}` (enterprise/user) or `/services/Soap/c/{version}` (partner/client) | Session-based enterprise/partner object operations; reads, writes, and deletes | `authenticated_request` with a SOAP XML body and `Content-Type`/`SOAPAction` headers | Build 5: typed Partner SOAP operations with no REST equivalent (for example `merge`, `convertLead`, `emptyRecycleBin`) |
| Metadata API | HTTPS SOAP, `/services/Soap/m/{version}` | `deploy`, `retrieve`, and `describeMetadata`; asynchronous metadata writes/reads | `authenticated_request` with a SOAP XML body | Build 5: typed `metadata` family (describeMetadata, listMetadata, readMetadata, deploy and checkDeployStatus, retrieve and checkRetrieveStatus) once zip package handling is contained like the CSV paths |
| Apex REST | HTTPS REST, user-defined `/services/apexrest/{namespace}/{resource}` | Custom Apex HTTP resources; direction is resource-defined and can read, write, or delete | `authenticated_request` with method/body/response mode | Org-defined resources have no fixed vendor shape; the generic verb is the intended route. Invocable Apex is typed through `actions` |
| Invocable Actions | HTTPS REST, `/services/data/v{version}/actions/standard` and `/actions/custom/{type}` | Discover and invoke standard actions, invocable Apex, autolaunched flows, quick actions and email alerts; invocation can write | `actions` verb: `list`, `list_standard`, `list_custom`, `list_custom_type`, `describe_standard`, `describe_custom`, `invoke_standard`, `invoke_custom`; results whose `isSuccess` is false are counted in `failures` | Covered (build 4) |
| Anonymous Apex | HTTPS REST Tooling action, `/services/data/v{version}/tooling/executeAnonymous` | Execute one-off Apex; write-capable side effects and diagnostic reads | `tooling` verb, operation `execute_anonymous` (`anonymousBody`); a compile or run failure is counted in `failures` with `compileProblem` and `exceptionMessage` in the body | Covered (build 3) |
| Reports | HTTPS REST, `/services/data/v{version}/analytics/reports/` and `/analytics/reportTypes` | List, create, clone, describe, run, save and delete reports; report types; reads, writes, and deletes | `reports` verb: `list`, `create`, `clone`, `describe`, `run`, `run_filtered`, `run_async`, `instances`, `instance_results`, `query`, `update`, `delete`, `report_types`, `describe_report_type`; a run with `allData` false returns `pagination.report_all_data` | Covered (build 4). Build 5: report instance delete once verified against the current guide; `authenticated_request` reaches it now |
| Dashboards | HTTPS REST, `/services/data/v{version}/analytics/dashboards/` | List, describe, refresh, clone, save, delete and obtain dashboard data; reads, writes, and deletes | `dashboards` verb: `list`, `clone`, `describe` (query `loadComponentProperties`), `results`, `refresh` and `status` (query `runningUser`, `filter1`, `filter2`, `filter3`), `update` (query `isStickyFilterSave`), `delete` | Covered (build 4). Not yet typed, reachable through `authenticated_request`: the dashboard components POST, filter options analysis and filter operators |
| Platform Events | REST publish at `/services/data/v{version}/sobjects/{EventApiName}__e`; subscription over CometD or the Pub/Sub API | Publish is a write; subscription is a streaming read | Publish: `create_record` with an `__e` sObject publishes one event, and `composite` `collections_create` publishes up to 200; `rest_resources` `describe` reads an event's or change event's fields | Publish and describe covered. Build 5: `platform_event_subscribe` (also Change Data Capture channels) needs a streaming-capable transport with replay and durable cursor evidence |

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
| `authenticated_request` | any | Every other relative endpoint: SOAP, Metadata, Apex REST and unwrapped REST/Tooling resources |
| `test_connection` | diagnostic read | Registered-org binding check |
| `probe_cli` | local diagnostic read | Local `sf` executable only |
| `provision_cli` | local system write | Install/bind the `sf` CLI; not a Salesforce API family |

The typed families call the same transport as `authenticated_request`, so
the registered-org binding, the refusal of caller `Authorization`/`Host`
headers, and the redaction of credential echoes in raw, JSON-escaped,
percent-encoded and base64 forms apply to every typed operation unchanged. A
typed operation adds what the generic verb cannot know: the documented method
and path, documented query parameter names and value types, required body
members, an `error_code` for a vendor HTTP failure, a `failures` count for
per-part outcomes inside a 2xx, and pagination hints. Because each typed
operation documents its response type, its JSON or Bulk CSV body is separated
from the CLI's printed headers by a proof that does not apply to the generic
verb (`request_actions._typed_boundary`).

## Read/write posture evidence

These are the posture sites that follow-on units must update or preserve.
Line references are to this checkout after build 4.

| Site | Evidence |
|---|---|
| `plugins/salesforce_plugin/plugin.yaml:3-8` | Plugin description lists SOQL including queryAll, record CRUD including delete, Bulk ingest and query jobs, composite, Tooling with anonymous Apex, Reports and Dashboards, REST resources, invocable actions, and the generic request |
| `plugins/salesforce_plugin/plugin.yaml:25-34` | Full R/W including delete under RATIFY-2 and USER ruling `rul_4c99a30d`; hardDelete, Collections deletes, Tooling deletes, anonymous Apex, report and dashboard deletes, external-id deletes and action invocation are first-class; remaining typed gaps are later builds reachable through `authenticated_request` |
| `plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md:24-50` | Overview posture: vendor permissions are the only boundary, no read-only mode, role request or confirmation prompt |
| `plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md:177-202` | The 23-verb inventory including the seven typed families and the generic request |
| `plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md:226-281` | Bulk ingest with first-class hardDelete and the `bulk_jobs` query lifecycle |
| `plugins/salesforce_plugin/knowledge_base/hydration_guidance.md:13` | Hydration presents creates, updates, permanent deletes, hardDelete, report and dashboard changes, invocable actions, Tooling changes and anonymous Apex as the operator's own user, and states there is no read-only integration user |
| `plugins/salesforce_plugin/knowledge_base/hydration_guidance.md:26` | Setup covers Bulk ingest (import and export roots) and Bulk query exports (export roots only) |
| `plugins/salesforce_plugin/src/salesforce_plugin/plugin.py:19-30` | Source-level verb posture, with no plugin-wide read-only gate |
| `plugins/salesforce_plugin/src/salesforce_plugin/typed_families.py:34-35` | Typed families classify nothing as read or write; Salesforce permissions decide every operation |
| `plugins/salesforce_plugin/src/salesforce_plugin/constants.py:100-101` | The Bulk ingest operation set includes `hardDelete` |
| `plugins/salesforce_plugin/src/salesforce_plugin/bulk_actions.py:69-78` | `hardDelete` maps to `sf data delete bulk --hard-delete`; no local refusal |
| `plugins/salesforce_plugin/tests/smoke_bulk.py:211` | Smoke coverage proves the hardDelete command line and that unknown operations are still refused |
| `ananta/knowledge_bases/ananta_platform/23_plugin_inventory/01_full_plugin_roster.md:120` | Roster records `salesforce_plugin` as active and full R/W including delete; its "`run_apex`/bulk ops/file verbs out of v1" note is stale after builds 3 and 4 and is outside this plugin's scope fence (named as a follow-up) |

There is no Salesforce plugin statement allowlist, method allowlist, or
read-only integration-user guard in the source. The remaining local limits are
target-org pinning, the refusal of caller credential/host headers, input shape
checks that mirror the vendor documents, and path containment for local
import/export files. Those are integrity boundaries, not reasons to mark an API
family unsupported.

## Build order

Writes and deletes stay first-class and retain the existing job ledger/audit
shape. Sizes are rough unit estimates, not a rescore of the registered plan.

| Order | Unit | Deliverable | Status |
|---:|---|---|:---:|
| 1 | Generic authenticated request | `authenticated_request` forwards method/path/query/headers and raw JSON/XML/text body through the pinned-org CLI, with deferred completion and redacted audit evidence (`unt_12041887-9b46-485d-a4e4-1d89e82c7d3a`) | Built |
| 2 | REST composite and queryAll | `composite` family and `include_deleted` on the SOQL verbs | Built (build 3) |
| 3 | Bulk API 2.0 completion | hardDelete, `bulk_jobs` query lifecycle, ingest list and delete | Built (build 3) |
| 4 | Tooling API and anonymous Apex | `tooling` family including `execute_anonymous` | Built (build 3) |
| 5 | Reports and Dashboards | Typed `reports` and `dashboards` families | Built (build 4) |
| 6 | REST remainder | `rest_resources`: relationship traversal, updated/deleted change reads, external-id get/upsert/delete, paged query and queryAll, query explain, limits, recent items, object info, search; Tooling `sobject_info` | Built (build 4) |
| 7 | Invocable actions | `actions`: standard and custom action discovery, describe and invoke | Built (build 4) |
| 8 | Metadata API | Typed `metadata` family over SOAP with contained zip packages | Build 5 |
| 9 | SOAP API | Partner SOAP operations with no REST equivalent (`merge`, `convertLead`, `undelete`, `emptyRecycleBin`) | Build 5 |
| 10 | Platform Event subscription | Streaming-capable subscribe with replay/cursor evidence, including Change Data Capture channels | Build 5 or later |
| 11 | REST and Tooling remainder | Tooling composite, Bulk parallel result pages, report instance delete, relationship writes, ContentVersion file upload and download | Build 5 |

For every write or delete unit, include authorization, optimistic-concurrency
premises where the vendor supports them (`If-Match`/`If-Unmodified-Since` pass
through the `headers` parameter), append-only job/audit history, current and
historical result readback, stale-write refusal (`sf.precondition_failed`),
reversal or abort semantics where available, and terminal completion behavior.

## Acceptance mapping

| Ticket acceptance clause | This inventory's implementation mapping |
|---|---|
| Every vendor API family has typed verbs or the generic request verb | Every named family appears in the coverage table with its typed verb or `authenticated_request` route. |
| `delete_record` succeeds against a scratch org | `delete_record` is the typed REST sObject delete path; `composite` `collections_delete`, `tooling` `delete` and `rest_resources` `delete_by_external_id` add batch, Tooling and external-id deletes. A scratch-org run needs a registered org and is not part of the hermetic smokes. |
| Bulk delete succeeds against a scratch org | `bulk_ingest_submit(operation=delete)` and `operation=hardDelete` are the typed Bulk v2 delete paths; a scratch-org run needs a registered org. |
| Coverage table exists in the plugin KB | This file is the table and is under `plugins/salesforce_plugin/knowledge_base/`. |
| No family is marked unsupported | Every gap is routed to `authenticated_request` with a named typed follow-up. |
| Hydration guidance no longer instructs a read-only integration user | `hydration_guidance.md:13` describes full operator read/write/delete access, including hardDelete and anonymous Apex, and states there is no read-only integration user. |

The vendor RBAC boundary remains authoritative: a typed or generic request may
reach only what the registered Salesforce credential is permitted to reach.
That is distinct from silently omitting an API family from the product
surface.
