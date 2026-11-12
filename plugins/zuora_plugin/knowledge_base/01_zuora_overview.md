# Zuora Plugin (`zuora_plugin`)

A subscription-billing connector over the operator's Zuora tenant. Executor:
a plain synchronous `httpx.Client` (no official Zuora SDK, no CLI). Auth:
OAuth 2.0 client-credentials — the simplest auth model of the platform's
connector suite. Single tenant v1: the "zuora_tenant" address-book entry.

## Vendor-controlled access

The registered Zuora vendor role is the only capability boundary. Grant it the
work the operator needs; the plugin adds no read-only posture, no allowlist and
no confirmation prompt of its own. Reads, writes and deletes are equally
first-class, and an operation the role lacks completes with Zuora's own 403
reason as `zuora.permission_denied`.

Current lifecycle inventory:

- **Workflows** (typed): list, read, correct (rename, set or clear the
  description), close and reopen (status Inactive/Active), roll back (switch
  the active version), delete; export to a JSON backup and import it again;
  list, import and delete versions; run, read and stop runs; list, read, rerun
  and batch-correct tasks; daily task usage; create, correct, deactivate and
  delete callout authentication providers.
- **SQL Data Query** (typed): submit, list, read, cancel (the job's terminal
  state) and download the result file.
- **Files** (typed): download, read status, restore from the archive.
- **Orders** (typed): preview (synchronous or async), create (synchronous or
  async; Draft, Pending, Completed or Scheduled), read, list for the tenant,
  a subscription owner, an invoice owner, a subscription or its pending
  orders, correct a draft or scheduled order, correct or clear custom fields
  and trigger dates, activate a draft, cancel a draft or scheduled order (its
  terminal state), delete (synchronous or async; Zuora rolls the changed
  subscriptions back), read delta metrics and async job status, rescan a
  commitment's account associations.
- **Subscriptions** (typed): preview new or existing, create, read the current
  or a historical version, amend, renew, suspend and resume (the vendor's
  close and reopen pair), cancel (terminal), delete, correct or clear a
  version's custom fields, read MRR/TCV metrics. `list_subscriptions` exports
  an account's subscriptions with the same charge-segment and metrics options.
- **Accounts** (typed): create (optionally with a payment method and first
  subscription), read basic info or the full summary, correct, delete
  (Zuora's asynchronous cancel-and-remove job), list payment methods, read the
  default payment method, read and set payment-method cascading.
- **Payment methods** (typed): create on an account or orphaned (card, card
  reference, bank debit, PayPal, Apple Pay, Google Pay, Amazon Pay), create
  from an encrypted Apple Pay or Google Pay token, read, correct or clear,
  verify with the gateway, read an ACH account's Plaid balance, scrub (the
  terminal erase of its sensitive data), delete (refused for the account's
  default); stored credential profiles: create, list, add gateway data,
  cancel or expire (their two terminal states); register, list and
  unregister wallet domains; create a Payment Form session.
- **Payments** (typed): create (external or electronic, optionally applied),
  list with filters and sort, read with its parts and item parts, correct or
  clear bookkeeping fields, apply and unapply (the reversible pair),
  transfer to another account, refund (optionally unapplying first), cancel
  (terminal Canceled) and delete (only a cancelled payment).
- **Custom objects** (typed): create, list, read, update (an asynchronous
  migration job) and delete definitions (refused while records exist);
  create, list, read, replace (with an optional `If-Match` precondition),
  patch (a `null` clears a field), delete and batch update or delete records;
  bulk jobs: submit, upload the `.csv`, list, read, list row errors, cancel.
- **Billing documents and invoices** (typed): list invoices, credit memos
  and debit memos together; generate an account's billing documents now;
  delete billing document files (a job) and read the job; create standalone
  invoices (one or up to 50), correct one or up to 50, post up to 50 and
  unpost (the reversible pair), split a draft into 2 to 20 invoices, email,
  read PDF status, list and upload PDF files, list items, item taxation items
  and application parts, add external taxation items, reverse (a credit memo
  is applied), write off (synchronously or as a job, read with its own verb),
  cancel (kept as Canceled) and delete.
- **Credit memos** (typed): create from catalog charges, from an invoice or
  up to 50 in bulk; list, read, correct one or up to 50; post and unpost;
  cancel (Draft only) and delete (Cancelled only); apply and unapply to
  invoices and debit memos (the reversible pair, also as jobs with their job
  reads); refund the unapplied part; reverse (a debit memo is created); write
  off a fully unapplied memo; email; generate and read PDF status; files,
  items, parts and taxation items.
- **Debit memos** (typed): create from catalog charges, from an invoice or up
  to 50 in bulk; list, read, correct one or up to 50, change due dates; post
  and unpost; cancel (Draft only) and delete (Cancelled only); collect
  (credits and unapplied payments, then a payment); write off; email;
  generate and read PDF status; files, items, taxation items and
  application parts.
- **Bill runs** (typed): create (ad hoc or scheduled), read, post a Completed
  run, cancel a Draft run (one occurrence or every future run), delete a
  Canceled or Error run, email its documents.
- **Usage** (typed): import a usage file (all or nothing; Zuora answers with a
  status link), create, read, correct and delete one usage record, read the
  rate detail of a usage-based invoice item.
- **Legacy catalog** (typed): products, product rate plans, product rate
  plan charges, product charge definitions and catalog groups: create, read,
  correct, clear mutable fields and delete; bulk charge definitions,
  effective-date retirement/restoration and reversible group membership.
  See [catalog lifecycle](catalog_lifecycle.md) for every route and vendor
  concurrency premise. The newer Commerce API stays on `request`.
- **Object Query** (typed, read-only by vendor design): list or read one
  record of any of Zuora's standard objects or a custom object with
  `filter[]`, `expand[]`, `sort[]` and field lists.
- **Objects and billing reads** (typed): create and update Account,
  Subscription, Invoice, Payment and Product; read one object or invoice
  (`get_invoice` takes the four documented v1 headers); list an account's
  subscriptions (with the same headers) or invoices; ZOQL `data_query` and
  `bulk_export`.
- **Everything else** goes through the generic `request` verb: any relative
  tenant path, any method, including vendor deletes and lifecycle actions.

Zuora defines some billing terminal states as void or cancel rather than
delete. Those keep the vendor's own semantics; the docs never call a void a
delete. Zuora publishes no ETag or version precondition on the Workflow, Data
Query, Files, Orders, Subscriptions, Accounts, Payment Methods, Payments,
billing document, memo, bill run, usage or legacy catalog routes, so concurrent corrections
are last-writer-wins: read current state
with the matching get or list verb before writing. The one documented
precondition, `If-Match` on a custom object record replace, is accepted as
`if_match`, and Zuora refuses a stale write. Where Zuora documents
`Idempotency-Key` (order, subscription, account, payment-method and payment
creates, refunds, stored credential profile actions, Workflow imports and
runs, async order delete, Data Query submit, invoice and memo creates,
emails, PDF generation, file uploads and taxation items, credit memo refunds,
debit memo collection, bill run create and email, usage import and create),
the verb accepts
`idempotency_key` so a caller can repeat a create safely; the plugin itself
never replays a write.

## Typed verbs (Workflows through Catalog)

The 28 legacy catalog operations live in
`plugins/zuora_plugin/src/zuora_plugin/catalog_actions.py`, with declarations
in `catalog_product_processes.py` and `catalog_definition_processes.py`.
Their request and lifecycle details are in [catalog lifecycle](catalog_lifecycle.md).

Every typed verb is one `Operation` in
`plugins/zuora_plugin/src/zuora_plugin/workflow_actions.py`,
`plugins/zuora_plugin/src/zuora_plugin/data_query_actions.py`,
`plugins/zuora_plugin/src/zuora_plugin/order_actions.py` (Orders and
Subscriptions), `plugins/zuora_plugin/src/zuora_plugin/account_actions.py`,
`plugins/zuora_plugin/src/zuora_plugin/payment_actions.py` (Payment Methods
and Payments), `plugins/zuora_plugin/src/zuora_plugin/custom_object_actions.py`
(Custom Objects and Object Query),
`plugins/zuora_plugin/src/zuora_plugin/billing_document_actions.py` (Billing
Documents, Invoices, Credit and Debit Memos) or
`plugins/zuora_plugin/src/zuora_plugin/bill_run_usage_actions.py` (Bill Runs
and Usage): the exact
vendor method and path, the caller parameters mapped onto path segments,
query keys and body, and Zuora's documented limits (workflow `page_length`
at most 50, task `page_length` at most 100, Data Query enums, DSV separator
rules, the reserved run-parameter words). The shared executor in
`plugins/zuora_plugin/src/zuora_plugin/typed_operations.py` validates before
any OAuth or vendor call, percent-encodes identifiers into their path
segment, sends exactly one request (a 401 re-mints the bearer once), maps a
non-2xx answer to a typed job error, and returns the vendor result as
received. Its audit line is built only from method, path without query values,
status, duration and correlation id.

The Orders, Subscriptions, Accounts, Payment Methods, Payments, Custom
Object, billing document, memo, bill run and usage verbs follow Zuora's v1 conventions
(`plugins/zuora_plugin/src/zuora_plugin/commerce_fields.py`):

- **Bodies.** The vendor's order, subscription or account document is passed
  as `body` and forwarded unchanged, so every documented field is accepted;
  the documented required top-level fields (for example `orderDate`,
  `cancellationPolicy`, or `name`/`currency`/`billToContact`) are checked
  before any vendor call.
- **Headers.** Each verb accepts exactly the request headers its vendor page
  documents: `zuora_version` (`Zuora-Version`, which changes response shapes),
  `zuora_entity_ids`, `zuora_org_ids`, `zuora_track_id` (US-ASCII without
  `: ; " '`) and, where documented, `idempotency_key`. The HTTP client
  negotiates `Accept-Encoding` itself. The same rule holds for the Workflow
  and Files verbs, whose vendor pages document a narrower set per route
  (most Workflow routes document no `Zuora-Version`; export, version and
  delete routes document only `Zuora-Track-Id`). The three PDF-status routes
  document no `Zuora-Org-Ids`; `create_usage`, `import_usage` and `get_usage`
  add `x_zuora_wsdl_version` (146 or higher selects the product rate plan
  charge usage fields).
- **`success: false`.** A v1 route can answer HTTP 200 with `success: false`
  (`Success: false` on the object usage routes);
  that completes the job as `zuora.validation_failed` with Zuora's `reasons`,
  never as a completed write.
- **Pagination.** Page-numbered lists (`list_orders` and its by-owner and
  by-subscription forms) need `page_size` before `page`, as Zuora documents;
  completion's `pagination.next_page` is the page number from Zuora's
  `nextPage` URL, null on the last page. The billing document, memo, item,
  part, file and taxation item lists page the same way. The invoice and debit
  memo application-parts routes answer a `nextPage` link but document no page
  parameter: completion's `pagination.next_path` is that link's tenant path,
  to fetch with `request`, null at the end.
- **Cursor lists.** Object Query, custom object records and bulk job lists
  page by cursor: completion's `pagination.next_cursor` is the value to pass
  back as `cursor` (records also need `page_size`), null at the end or when
  Zuora returned fewer records than `page_size`. Object Query `page_size` is
  1 to 99.
- **Results and log hygiene.** Results are not scanned: every verb returns
  the vendor result as received, and bulk results go to disk through
  `output_path`. Audit and log lines are built only from method, path without
  query values, status, duration and correlation id, so a payment instrument a
  caller embeds in a body never reaches a log line.
- **Uploads.** `upload_custom_object_job_file` reads one `.csv` from
  `input_path` under a saved location and sends it as `text/csv`.
  `upload_invoice_file`, `upload_credit_memo_file` and
  `upload_debit_memo_file` send one `.pdf`, and `import_usage` one `.csv`,
  `.xls`, `.xlsx` or `.zip`, as the `file` part of a multipart/form-data body
  framed with a fresh boundary that does not occur in the file.

Workflow lists return one page plus `pagination.next_page` (the next page
number while Zuora's `pagination.next_page` is present, otherwise null).
`list_data_query_jobs` has no vendor continuation: its `pagination` reports
`returned`, `page_size` and `may_have_more` (true when the response filled
`page_size`) with a hint to filter by status or raise `page_size`. `export_workflow`
can write its JSON to an `output_path` backup; `import_workflow` and
`import_workflow_version` read one back from `input_path`. Both paths, and
the download verbs' `output_path`, must sit under a saved location and
are checked before any vendor call. `download_data_query_result` reads the
job and fetches the pre-signed `dataFile` URL Zuora itself issued, never a
caller URL, and never sends the bearer token there; a job that has not
completed returns `downloaded: false` with its `query_status`.

A tenant Workflow backup runs end to end through the plugin: `list_workflows`
page by page, `export_workflow` with an `output_path` per workflow (and per
version from `list_workflow_versions` when the history matters), then
`import_workflow` or `import_workflow_version` from those files to restore.
The client secret never leaves the address book.

## Session model — re-mintable bearer, in-memory only

The OAuth `client_secret` is the durable credential (chain-consumed through
the address book, never vaulted under this plugin's own identity). The
bearer token it mints is short-lived (~1h), re-mintable, and held ONLY in
process memory (`http_client.py::ZuoraClient`) — never persisted. A 401
triggers exactly ONE re-fetch-and-retry; a second 401 classifies as
`zuora.auth_failed`.

## Registering the OAuth client (operator console runbook, one-time)

1. **Create an OAuth client** in the Zuora tenant (Zuora admin console →
   Administration → Manage OAuth Clients) on a platform/API user with the
   permissions this connector needs (Data Query, Object API, Billing).
2. **Copy the client_id and client_secret** — Zuora surfaces the secret only
   once at creation.
3. **Register the address-book entry** `zuora_tenant` with the literal
   fields `base_url` (the environment selector — see below) and `client_id`,
   and a `client_secret` field holding
   `vault::<solet>.default_address_book_plugin.zuora_client_secret`.
   Ingest the secret agent-blind.

**`base_url` IS the environment selector** — there is no separate
prod/sandbox flag:

| Environment | `base_url` |
|---|---|
| US Production | `https://rest.zuora.com` |
| EU Production | `https://rest.eu.zuora.com` |
| Sandbox | `https://rest.apisandbox.zuora.com` |

## Business-data limits + data-export migration (2026-08-02)

`data_query`, `bulk_export`, `list_subscriptions`, and `list_invoices` ALWAYS
write their result to the caller's `output_tsv_path` — never records inline,
at any size (`workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md`;
the former blob-export/`INLINE_BYTE_CAP` branch on `data_query`/`bulk_export`
is deleted, not lowered). Neither destination is platform blob storage — the
path must be absolute, end in `.tsv`, and lie under a saved path address, the session folder or solet home; the installation tree is always excluded. `get_object`/`get_invoice` (single-record
fetch-by-id) are unaffected and stay inline.

Each of the four defaults to 500 records absent an acknowledged override:

| Verb | Params | Override ceiling | Vendor mechanism |
|---|---|---|---|
| `data_query` | `zoql`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 1000 | Single `POST /v1/action/query` call (vendor cap 2000/call) |
| `bulk_export` | `zoql`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 50000 (the N>>500 route) | `POST /v1/action/query` + `POST /v1/action/queryMore` continuation loop, following the vendor's own `queryLocator`/`done` signal past the 2000/call ceiling |
| `list_subscriptions` | `account_id`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 5000 | Internal `page`/`pageSize` pagination against `GET /v1/subscriptions/accounts/{account_id}` (vendor cap 40/call, documented `GET_SubscriptionsByAccount` operation) |
| `list_invoices` | `account_id`, `output_tsv_path`, `acknowledge_default_limit_override=false`, `row_limit?` | 5000 | Single call to `GET /v1/invoices/accounts/{account_id}`; `row_limit` caps what is WRITTEN, not a confirmed pre-fetch ceiling — see the note below |

`acknowledge_default_limit_override=true` together with an explicit
`row_limit` requests more than the default — both are required together, and
a `row_limit` above the verb's hard cap is refused, never silently clamped.
Nested objects (e.g. a subscription's rate plans) serialize as JSON text in
their TSV cells.

**`data_query`/`bulk_export` fixed a pre-existing, independently-confirmed
defect**, not just added the override mechanism: `bulk_export`'s
`BULK_EXPORT_ROW_CAP` (50,000) was vacuous — the verb posted to the same
synchronous query endpoint as `data_query` and never called `queryMore`, so
the vendor's own ~2,000-record ceiling was hit first every time and the
50,000 figure could never be reached. Both verbs now call the documented,
current Zuora Actions pair (`POST /v1/action/query` / `POST /v1/action/queryMore`,
operationIds `Action_POSTquery`/`Action_POSTqueryMore`) rather than the
undocumented legacy `/v1/query` alias this module used previously — the same
underlying ZOQL mechanism, but with a citable current contract for the
queryMore continuation this fix depends on.

**`list_subscriptions` fixed a second pre-existing defect: silent
truncation.** `GET /v1/subscriptions/accounts/{account_id}` is a documented,
page/pageSize-paginated endpoint (component `GLOBAL_REQUEST_pageSize`: max
40, default 20) — the prior implementation passed neither parameter, so it
silently returned at most 20 subscriptions with no signal more existed. This
build pages internally up to the effective row limit.

**`list_invoices`' endpoint is NOT independently confirmed.**
`GET /v1/invoices/accounts/{account_id}` (like the legacy `/v1/query` alias
above) is absent from Zuora's current published OpenAPI bundle, but unlike
`/v1/query` — which is independently verified live via `data_query`'s own
2,000-record-cap behavior — there is no equivalent live-behavior evidence for
this endpoint, and no documented pagination contract to build against.
Zuora's documented CURRENT account-scoped billing-document listing,
`GET /v1/billing-documents`, mixes invoices with credit/debit memos and has
no `documentType` query filter — migrating to it is a verb-contract change
outside this wave's approved scope (flagged for a ruling separately, not
built silently). `list_invoices` therefore still issues a single call and
applies `row_limit` as a cap on what is WRITTEN, disclosed as such in the
process description rather than presented as a confirmed guarantee. Build 5
added the typed `list_billing_documents` for the current route (page-numbered,
filtered by account, document date or status, each row carrying its
`documentType`), so a caller who needs Zuora's documented listing uses that
verb; `list_invoices` keeps its TSV export contract unchanged.

New error code: `zuora.export_path_refused` (path not absolute / not `.tsv` /
not contained under any saved location).

## Security posture (mirrors the Jira/Snowflake/Salesforce/external-Postgres wave)

- **Foreign-target invariant.** No `base_url`/environment parameter on any
  verb — every request is built from the single registered `zuora_tenant`
  entry. The one off-tenant fetch is a completed SQL Data Query job's
  pre-signed `dataFile`, read from Zuora's own job record and fetched with no
  Authorization header and no redirect following.
- **No export deny.** Not denied from the MCP surface — call `process_call` on
  these verbs directly. The prior RATIFY-3 deny was retired by operator ruling
  2026-07-15: friction, not security, on a single-user substrate where every
  MCP session is the operator (see
  `workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
- **Generic error messages for topology-leaking classes.** Auth and
  rate-limit errors NEVER embed the raw response body or request URL.
  Object-not-found, validation-failed, query-failed and permission-denied
  errors describe the caller's own object/query via the vendor's message
  fields only (`reasons[].message`, Workflow `errors`/`Errors`, Data Query
  `message`), as received.
- **Secrets hygiene.** The OAuth client_secret is chain-consumed through the
  address book's `resolve_with_secrets` — this plugin declares no vault keys
  of its own (`get_required_vault_keys`/`get_declared_vault_keys` both
  return `[]`). The re-minted bearer token is never vaulted.
- **Financial-data sensitivity.** Zuora records (invoices, payments,
  subscriptions) carry real billing PII — this connector's field
  sensitivities are set at the DB-connector floor (0.5), one notch above the
  generic-SaaS floor (0.3) used for Jira/Salesforce records, reflecting the
  higher consequence of a leaked invoice or payment row.

## SQL-lockdown gate note

`src/` composes NO SQL-shaped strings anywhere — ZOQL queries flow through as
a caller-supplied JSON body field (`queryString`), never a literal string the
gate's S2 heuristic would match, and there is no database driver import
(`S0`). The gate's S2 heuristic DOES fire on the ZOQL-shaped literal test
fixtures in `tests/smoke_data_query.py` (query strings like `"SELECT Id, Name
FROM Account"` standing alone as a Python string literal) — foreign-tenant
ZOQL text, never platform SQL, allowlisted in
`quality_gates/sql_access_allowlist.txt`.

## Design provenance

This plugin predates the 2026-07-09 umbrella hardening pass that ratified
Snowflake/Salesforce/Jira/external-Postgres (`workbench/2026-07-09_enterprise_connectors_design.md`,
which explicitly marks Zuora "out of scope, not in the operator's four").
The original design (`workbench/2026-06-20_zuora_plugin_design.md`) is a
single, lighter-weight revision. This build applies the umbrella's since-
established security posture (generic topology-safe error messages,
chain-consumed secrets, sync verb shape) on top of that original design
rather than re-litigating it from scratch. (The umbrella's process_export
deny posture was removed platform-wide 2026-07-15 — see the Security
posture section above.)

## Key files

| File | Purpose |
|---|---|
| `src/zuora_plugin/constants.py` | Every magic value: address-book field names, known base_url environments, error codes, caps. |
| `src/zuora_plugin/app_config.py` | Resolves `zuora_tenant` from the address book; the client_secret is chain-consumed. |
| `src/zuora_plugin/http_client.py` | `ZuoraClient` — synchronous httpx client with cached, re-mintable bearer auth (client-credentials grant). |
| `src/zuora_plugin/errors.py` | Topology-safe error classification (`classify_zuora_response`) from the response body's `reasons` list. |
| `ananta/src/ananta/file_locations/containment.py` | Shared `assert_path_allowed` guard using saved path addresses, the session folder and solet home — admits `output_tsv_path` for `data_query`/`bulk_export`/`list_subscriptions`/`list_invoices`. |
| `src/zuora_plugin/billing_actions.py` | Pure verb implementations: `data_query`, `get_object`, `create_object`, `update_object`, `list_subscriptions`, `get_invoice`, `list_invoices`, `bulk_export` — the latter four write to `output_tsv_path` under the §5 override mechanism (see the migration section above). |
| `src/zuora_plugin/plugin.py` | The `ZuoraPlugin` EDGE provider — client lifecycle, error mapping, EDGE registration, `_export_path_gate`. |
| `plugins/zuora_plugin/src/zuora_plugin/typed_operations.py` | Shared executor for typed vendor operations: validation, path encoding, one call, error mapping, audit log, pagination. |
| `plugins/zuora_plugin/src/zuora_plugin/workflow_actions.py` | The 20 Workflow operations (definitions, export/import, versions, runs, tasks, usage, auth providers). |
| `plugins/zuora_plugin/src/zuora_plugin/data_query_actions.py` | SQL Data Query jobs and Files operations, including result and file downloads. |
| `plugins/zuora_plugin/src/zuora_plugin/typed_verbs.py` | Registry tying each typed verb to its worker handler, dispatch validator, retry class and result type. |
| `plugins/zuora_plugin/src/zuora_plugin/workflow_processes.py`, `plugins/zuora_plugin/src/zuora_plugin/data_query_processes.py` | The `@platform_process` declarations mixed into `ZuoraPlugin`. |
