# Zuora Vendor API Coverage

Article Layer: 1

Article Role: build-specification

Article Tags: planning-stage:solet-lifecycle, domain:zuora, domain:connector,
evidence-category:api-inventory

Embedding Description: Complete Zuora REST-family inventory for extending
`zuora_plugin` to the registered OAuth client's vendor-RBAC boundary, including
reads, writes, deletes, workflows, billing, catalog, usage, custom objects,
ZOQL, SQL Data Query jobs, and the generic authenticated request path.

## Scope and evidence rule

This is the specification for the Zuora coverage build units. It covers the
families named by the feature request plus the adjacent tenant/configuration
families needed by the requested Workflow backup workflow. A family is never
called unsupported: it is either mapped to an existing verb, assigned a typed
verb, or assigned the one generic authenticated request verb.

The repository evidence is the current plugin source and process KB. Vendor
routes and operation sets below are anchored to the [Zuora v1 API
reference](https://developer.zuora.com/v1-api-reference/api), which is versioned
by Zuora and must be rechecked when a unit implements a route. Where this
inventory cannot establish an exact operation or payload, it says **to verify
against the Zuora v1 API reference** rather than guessing an endpoint.

The registered OAuth client's Zuora role is the only authorization boundary.
The plugin must not add a read-only, write, or delete allowlist. It must still
preserve the existing foreign-target invariant: the tenant comes only from the
`zuora_tenant` address-book entry; callers pass a relative vendor path, never a
new `base_url`.

## Current plugin surface

The plugin registers 227 EDGE verbs. Each has a `@platform_process`
declaration, an entry in `get_edge_process_definitions`
(`plugins/zuora_plugin/src/zuora_plugin/plugin.py`) and a process document
under `plugins/zuora_plugin/knowledge_base/processes/`.

| Current verb | Direction | Current vendor route or operation |
| --- | --- | --- |
| `request` | read/write/delete | Any method on any relative tenant path (build 1) |
| `test_connection` | read/auth | OAuth token mint |
| `get_object` | read | `GET /v1/object/{type}/{id}` |
| `create_object` | write | `POST /v1/object/{type}` |
| `update_object` | write | `PUT /v1/object/{type}/{id}` |
| `list_subscriptions` | read/export | `GET /v1/subscriptions/accounts/{account-key}` with `page`/`pageSize`, plus the documented `charge-detail`, `as-of-date`, `exclude-rate-plans-with-no-charges`, `getDetailedMetrics` and `asOfDay` read options (build 3) and the four documented v1 request headers (build 5) |
| `get_invoice` | read | `GET /v1/invoices/{invoiceKey}` (id or number), with the four documented v1 request headers (build 5) |
| `list_invoices` | read/export | `GET /v1/invoices/accounts/{account-key}`; pagination is explicitly unconfirmed in `billing_actions.py`; the documented listing is `list_billing_documents` |
| `data_query` | read/export | ZOQL `POST /v1/action/query` plus continuation |
| `bulk_export` | read/export | ZOQL query plus `queryMore` |
| `list_workflows`, `get_workflow` | read | `GET /workflows` (page, page_length ≤ 50), `GET /workflows/{workflow_id}` |
| `update_workflow`, `delete_workflow` | write/delete | `PATCH /workflows/{workflow_id}`, `DELETE /workflows/{workflow_id}` |
| `export_workflow`, `import_workflow` | read/write | `GET /workflows/{workflow_id}/export`, `POST /workflows/import` |
| `list_workflow_versions`, `import_workflow_version`, `delete_workflow_version` | read/write/delete | `GET /workflows/{workflow_id}/versions`, `POST /workflows/{workflow_id}/versions/import`, `DELETE /workflows/versions/{version_id}` |
| `run_workflow`, `get_workflow_run`, `stop_workflow_run` | write/read/terminal | `POST /workflows/{workflow_id}/run`, `GET /workflows/workflow_runs/{workflow_run_id}`, `PUT /workflows/workflow_runs/{workflow_run_id}/stop` |
| `list_workflow_tasks`, `get_workflow_task`, `rerun_workflow_task`, `update_workflow_tasks` | read/write | `GET /workflows/tasks` (page_length ≤ 100), `GET /workflows/tasks/{task_id}`, `POST /workflows/tasks/{task_id}/rerun`, `PUT /workflows/tasks/batch_update` |
| `get_workflow_usage` | read | `GET /workflows/metrics.json` |
| `create_workflow_auth_provider`, `update_workflow_auth_provider`, `delete_workflow_auth_provider` | write/delete | `POST /auth_providers`, `PUT /auth_providers/{provider_id}`, `DELETE /auth_providers/{provider_id}` |
| `submit_data_query`, `list_data_query_jobs`, `get_data_query_job`, `cancel_data_query_job` | write/read/terminal | `POST /query/jobs`, `GET /query/jobs`, `GET /query/jobs/{job-id}`, `DELETE /query/jobs/{job-id}` |
| `download_data_query_result` | read/export | `GET /query/jobs/{job-id}`, then the vendor-issued pre-signed `dataFile` URL without the bearer |
| `download_file`, `get_file_status`, `restore_file` | read/write | `GET /v1/files/{file-id}`, `GET /v1/files/{file-id}/status`, `PUT /v1/files/{file-id}/restore`, with the four documented v1 request headers (build 4) |
| `preview_order`, `preview_order_async`, `create_order`, `create_order_async`, `get_async_order_job` | write/read | `POST /v1/orders/preview`, `POST /v1/async/orders/preview`, `POST /v1/orders`, `POST /v1/async/orders`, `GET /v1/async-jobs/{jobId}` |
| `list_orders`, `list_orders_by_subscription_owner`, `list_orders_by_subscription`, `list_orders_by_invoice_owner`, `list_pending_orders`, `get_order`, `get_order_delta_metrics` | read | `GET /v1/orders`, `GET /v1/orders/subscriptionOwner/{accountNumber}`, `GET /v1/orders/subscription/{subscriptionNumber}`, `GET /v1/orders/invoiceOwner/{accountNumber}`, `GET /v1/orders/subscription/{subscription-key}/pending`, `GET /v1/orders/{orderNumber}`, `GET /v1/orders/deltaMetrics/{orderNumber}` |
| `update_order`, `update_order_custom_fields`, `update_subscription_custom_fields`, `update_order_trigger_dates`, `activate_order`, `cancel_order`, `rescan_commitment_associations` | write/terminal | `PUT /v1/orders/{orderNumber}`, `PUT /v1/orders/{orderNumber}/customFields`, `PUT /v1/subscriptions/{subscriptionNumber}/customFields`, `PUT /v1/orders/{orderNumber}/triggerDates`, `PUT /v1/orders/{orderNumber}/activate`, `PUT /v1/orders/{orderNumber}/cancel`, `POST /v1/orders/commitments/{commitmentNumber}/associations/rescan` |
| `delete_order`, `delete_order_async` | delete | `DELETE /v1/orders/{orderNumber}`, `DELETE /v1/async/orders/{orderNumber}` |
| `preview_subscription`, `preview_existing_subscription`, `create_subscription`, `get_subscription`, `get_subscription_version`, `get_subscription_metrics` | write/read | `POST /v1/subscriptions/preview`, `POST /v1/subscriptions/{subscription-key}/preview`, `POST /v1/subscriptions`, `GET /v1/subscriptions/{subscription-key}`, `GET /v1/subscriptions/{subscription-key}/versions/{version}`, `GET /v1/subscriptions/subscription-metrics` |
| `update_subscription`, `renew_subscription`, `suspend_subscription`, `resume_subscription`, `cancel_subscription`, `delete_subscription`, `update_subscription_version_custom_fields` | write/terminal/delete | `PUT /v1/subscriptions/{subscription-key}` and its `/renew`, `/suspend`, `/resume`, `/cancel` and `/delete` actions, `PUT /v1/subscriptions/{subscriptionNumber}/versions/{version}/customFields` |
| `create_account`, `get_account`, `get_account_summary`, `update_account`, `delete_account` | write/read/delete | `POST /v1/accounts`, `GET /v1/accounts/{account-key}`, `GET /v1/accounts/{account-key}/summary`, `PUT /v1/accounts/{account-key}`, `DELETE /v1/accounts/{account-key}` |
| `list_account_payment_methods`, `get_account_default_payment_method`, `get_account_payment_method_cascading`, `update_account_payment_method_cascading` | read/write | `GET /v1/accounts/{account-key}/payment-methods`, `.../payment-methods/default`, `GET` and `PUT .../payment-methods/cascading` |
| `create_payment_method`, `decrypt_payment_method`, `get_payment_method`, `update_payment_method`, `verify_payment_method`, `get_payment_method_balance`, `scrub_payment_method`, `delete_payment_method` | write/read/terminal/delete | `POST /v1/payment-methods`, `POST /v1/payment-methods/decryption`, `GET`, `PUT` and `DELETE /v1/payment-methods/{payment-method-id}`, its `PUT .../verify`, `GET .../balance`, `PUT .../scrub` (build 4) |
| `list_stored_credential_profiles`, `create_stored_credential_profile`, `add_stored_credential_profile_data`, `cancel_stored_credential_profile`, `expire_stored_credential_profile` | read/write/terminal | `GET` and `POST /v1/payment-methods/{payment-method-id}/profiles`, `POST .../profiles/{profile-number}/profile-data`, `/cancel`, `/expire` (build 4) |
| `list_wallet_domains`, `register_wallet_domain`, `unregister_wallet_domain`, `create_payment_session` | read/write/delete | `GET` and `POST /v1/payment-methods/wallet/domains`, `DELETE .../wallet/domains/{id}`, `POST /web-payments/sessions` (build 4) |
| `list_payments`, `create_payment`, `get_payment`, `update_payment`, `apply_payment`, `unapply_payment`, `transfer_payment`, `refund_payment`, `refund_payment_with_auto_unapply`, `cancel_payment`, `delete_payment` | read/write/terminal/delete | `GET` and `POST /v1/payments`, `GET`, `PUT` and `DELETE /v1/payments/{paymentKey}`, its `PUT /apply`, `/unapply`, `/transfer`, `/cancel`, `POST /refunds`, `POST /refunds/unapply` (build 4) |
| `list_payment_parts`, `get_payment_part`, `list_payment_item_parts`, `get_payment_item_part` | read | `GET /v1/payments/{paymentKey}/parts`, `.../parts/{partid}`, `.../parts/{partid}/item-parts`, `.../item-parts/{itempartid}` (build 4) |
| `create_custom_object_definitions`, `list_custom_object_definitions`, `get_custom_object_definition`, `update_custom_object_definition`, `delete_custom_object_definition` | write/read/delete | `POST` and `GET /objects/definitions/default`, `GET` and `DELETE /objects/definitions/default/{object}`, `POST /objects/migrations` (build 4) |
| `create_custom_object_records`, `list_custom_object_records`, `get_custom_object_record`, `update_custom_object_record`, `patch_custom_object_record`, `delete_custom_object_record`, `batch_custom_object_records` | write/read/delete | `POST` and `GET /objects/records/default/{object}`, `GET`, `PUT`, `PATCH` and `DELETE .../{object}/{id}`, `POST /objects/batch/default/{object}` (build 4) |
| `submit_custom_object_job`, `upload_custom_object_job_file`, `list_custom_object_jobs`, `get_custom_object_job`, `list_custom_object_job_errors`, `cancel_custom_object_job` | write/read/terminal | `POST` and `GET /objects/jobs`, `POST /objects/jobs/{id}/files` (`text/csv`), `GET /objects/jobs/{id}`, `GET .../errors`, `PATCH .../cancel` (build 4) |
| `query_objects`, `query_object` | read | `GET /object-query/{object}` and `GET /object-query/{object}/{key}` for every standard object and `{namespace}__{object}s` custom object (build 4) |
| `list_billing_documents`, `generate_billing_documents`, `create_billing_document_files_deletion_job`, `get_billing_document_files_deletion_job` | read/write/delete | `GET /v1/billing-documents`, `POST /v1/accounts/{key}/billing-documents/generate`, `POST` and `GET /v1/accounts/billing-documents/files/deletion-jobs[/{jobId}]` (build 5) |
| `create_standalone_invoice`, `create_standalone_invoices`, `update_invoice`, `update_invoices`, `post_invoices`, `unpost_invoice`, `split_invoice`, `email_invoice`, `get_invoice_pdf_status` | write/read | `POST /v1/invoices`, `POST /v1/invoices/batch`, `PUT /v1/invoices/{invoiceKey}`, `PUT /v1/invoices`, `POST /v1/invoices/bulk-post`, `PUT .../unpost`, `PUT .../split`, `POST .../emails`, `GET /v1/invoices/pdf-status` (build 5) |
| `reverse_invoice`, `write_off_invoice`, `write_off_invoice_async`, `get_invoice_write_off_async_job`, `cancel_invoice`, `delete_invoice` | terminal/delete | `PUT /v1/invoices/{invoiceKey}/reverse`, `PUT .../write-off`, `POST .../write-off-async`, `GET /v1/invoices/write-off-async-jobs/{asyncJobId}`, `PUT .../cancel`, `DELETE /v1/invoices/{invoiceKey}` (build 5) |
| `list_invoice_items`, `list_invoice_item_taxation_items`, `create_invoice_taxation_items`, `list_invoice_application_parts`, `list_invoice_files`, `upload_invoice_file` | read/write | `GET /v1/invoices/{invoiceKey}/items`, `.../items/{itemId}/taxation-items`, `POST .../taxation-items`, `GET .../application-parts`, `GET` and `POST .../files` (multipart PDF) (build 5) |
| 31 credit memo verbs: `list_credit_memos`, `create_credit_memo`, `create_credit_memo_from_invoice`, `create_credit_memos`, `update_credit_memos`, `get_credit_memo`, `update_credit_memo`, `post_credit_memo`, `unpost_credit_memo`, `cancel_credit_memo`, `delete_credit_memo`, `apply_credit_memo`, `unapply_credit_memo`, `apply_credit_memo_async`, `unapply_credit_memo_async`, `get_credit_memo_apply_async_job`, `get_credit_memo_unapply_async_job`, `refund_credit_memo`, `reverse_credit_memo`, `write_off_credit_memo`, `email_credit_memo`, `generate_credit_memo_pdf`, `get_credit_memo_pdf_status`, `list_credit_memo_files`, `upload_credit_memo_file`, `list_credit_memo_items`, `get_credit_memo_item`, `list_credit_memo_item_taxation_items`, `create_credit_memo_taxation_items`, `list_credit_memo_parts`, `get_credit_memo_part` | read/write/terminal/delete | Every route in the vendor's Credit Memos section under `/v1/credit-memos` (build 5) |
| 24 debit memo verbs: `list_debit_memos`, `create_debit_memo`, `create_debit_memo_from_invoice`, `create_debit_memos`, `update_debit_memos`, `update_debit_memo_due_dates`, `get_debit_memo`, `update_debit_memo`, `post_debit_memo`, `unpost_debit_memo`, `cancel_debit_memo`, `delete_debit_memo`, `collect_debit_memo`, `write_off_debit_memo`, `email_debit_memo`, `generate_debit_memo_pdf`, `get_debit_memo_pdf_status`, `list_debit_memo_application_parts`, `list_debit_memo_files`, `upload_debit_memo_file`, `list_debit_memo_items`, `get_debit_memo_item`, `list_debit_memo_item_taxation_items`, `create_debit_memo_taxation_items` | read/write/terminal/delete | Every route in the vendor's Debit Memos section under `/v1/debit-memos` (build 5) |
| `create_bill_run`, `get_bill_run`, `post_bill_run`, `cancel_bill_run`, `delete_bill_run`, `email_bill_run_documents`, `create_billing_preview_run`, `get_billing_preview_run` | write/read/terminal/delete | `POST /v1/bill-runs`, `GET` and `DELETE /v1/bill-runs/{billRunId}`, `PUT .../post`, `PUT .../cancel`, `POST /v1/bill-runs/{billRunKey}/emails`, `POST /v1/billing-preview-runs`, `GET /v1/billing-preview-runs/{billingPreviewRunId}` (build 5) |
| `import_usage`, `create_usage`, `get_usage`, `update_usage`, `delete_usage`, `get_usage_rate_detail` | write/read/delete | `POST /v1/usage` (multipart file), `POST /v1/object/usage`, `GET`, `PUT` and `DELETE /v1/object/usage/{id}`, `GET /v1/invoices/invoice-item/{invoice-item-id}/usage-rate-detail` (build 5) |

The typed Workflow, SQL Data Query and Files routes were checked against the
Zuora v1 API reference markdown (reference version 2026-11-06) by build 2.
Build 3 checked every operation in the vendor's Orders, Subscriptions and
Accounts sections against the same reference version: path, method, query
parameters, documented request headers and required top-level body fields.
Those verbs take the vendor document as `body` (every documented field
accepted), accept the documented `Zuora-Version`, `Zuora-Entity-Ids`,
`Zuora-Org-Ids`, `Zuora-Track-Id` and (where documented) `Idempotency-Key`
headers, and report an HTTP 200 carrying `success: false` as
`zuora.validation_failed` with Zuora's reasons. Build 3 also added the
documented Data Query headers to the five Data Query verbs.
Build 4 checked every operation in the vendor's Payment Methods (17),
Payments (15), Custom Object Definitions (5), Custom Object Records (7) and
Custom Object Jobs (6) sections, and both Object Query route shapes (98
documented GET routes, including the custom-object pair), against the same
reference version, plus the Object Query guide "Expand, filter, fields, and
sort". It also gave the 20 Workflow verbs and the 3 Files verbs exactly the
request headers their vendor pages document (Workflow routes document a
narrower, per-route set: most have no `Zuora-Version`).
Build 5 checked every operation in the vendor's Billing Documents (4),
Invoices (22), Credit Memos (31), Debit Memos (24), Bill Run (6), Billing
Preview Run (2) and Usage (6) sections against the same reference version:
path, method, query parameters, documented request headers and required
top-level body fields (95 operation pages, 0 mismatches; headers named only
in a page's prose count as documented too, so `import_usage` and `get_usage`
take `X-Zuora-WSDL-Version` like `create_usage`; `GET
/v1/invoices/{invoiceKey}` is the existing `get_invoice`, which now takes the
documented headers like `list_subscriptions`). The four file routes send
Zuora's multipart/form-data `file` part; the three PDF-status routes take at
most 50 keys and document no `Zuora-Org-Ids`; the two application-parts
routes answer a `nextPage` link without a documented page parameter, so the
verb returns that link's tenant path for `request`.
`get_object`, `create_object`, and `update_object` accept only `Account`,
`Subscription`, `Invoice`, `Payment`, and `Product`
(`plugins/zuora_plugin/src/zuora_plugin/constants.py`, `SUPPORTED_OBJECT_TYPES`).
That is a typed input check, not a boundary: `request` reaches every other
object.

## API-family inventory and implementation map

Base paths are relative to the registered tenant URL. “Delete” includes a
vendor delete operation where the family publishes one; it does not mean that
every billing record is physically deletable. For stateful records, build units
must also expose the vendor's close/cancel/reopen or reversal operation when it
exists, and document a vendor-defined terminal state when it does not.

| Family | Protocol and base paths | Vendor operation shape | Current plugin coverage | Gap and next verb |
| --- | --- | --- | --- | --- |
| Accounts | HTTPS REST; `/v1/accounts`, `/v1/accounts/{account-key}/summary`, `/v1/accounts/{account-key}/payment-methods` (list, default, cascading), `/object-query/accounts` | All 9 operations in the vendor's Accounts section: create, read, summary, update, delete, payment-method list, default payment method, cascading read and set. The section publishes no close/reopen route; delete is asynchronous (`jobId`, `jobStatus: Pending`), cancels the account and removes its transactions, and is refused for split-owner subscriptions and owner transfers. Verified against the [Accounts reference](https://developer.zuora.com/v1-api-reference/api/accounts), version 2026-11-06. | Typed (build 3): the 9 account verbs; `get_object`/`create_object`/`update_object` also reach `Account` | None in this section. The `/v1/payment-methods` lifecycle is typed in the Payments row (build 4); Object Query reads of accounts use `query_objects`/`query_object` (build 4). |
| Subscriptions | HTTPS REST; `/v1/subscriptions`, `/v1/subscriptions/preview`, `/v1/subscriptions/{subscription-key}` and its `/versions/{version}`, `/preview`, `/renew`, `/cancel`, `/resume`, `/suspend`, `/delete` actions, `/v1/subscriptions/subscription-metrics`, `/v1/subscriptions/accounts/{account-key}` | All 14 operations in the vendor's Subscriptions section: preview new and existing, create, list by account, read current and historical version, update, renew, cancel (terminal per `cancellationPolicy`), suspend and resume (the close/reopen pair), delete, version custom fields, metrics. Verified against the [Subscriptions reference](https://developer.zuora.com/v1-api-reference/api/subscriptions), version 2026-11-06. | Typed (build 3): 13 subscription verbs plus `list_subscriptions` (TSV export, with every documented read option and, since build 5, the documented request headers) | None. |
| Orders and order previews | HTTPS REST; `/v1/orders`, `/v1/async/orders`, `/v1/async-jobs/{jobId}`, order owner/subscription/pending lists, `/customFields`, `/triggerDates`, `/activate`, `/cancel`, delta metrics, commitment association rescan | All 21 operations in the vendor's Orders section: preview and create (sync and async), async job status, list (tenant, subscription owner, invoice owner, subscription, pending), read, update (draft or scheduled, whole-order replace), custom fields for orders and subscriptions, trigger dates, activate (draft), cancel (draft or scheduled, terminal), delete (sync and async; subscriptions roll back, refused once invoiced), delta metrics, commitment rescan. Verified against the [Orders reference](https://developer.zuora.com/v1-api-reference/api/orders), version 2026-11-06. | Typed (build 3): the 21 order verbs | None. |
| Invoices and billing documents | HTTPS REST; `/v1/billing-documents`, `/v1/accounts/{key}/billing-documents/generate`, `/v1/accounts/billing-documents/files/deletion-jobs`, `/v1/invoices`, `/v1/credit-memos`, `/v1/debit-memos` | All 81 operations in the vendor's Billing Documents (4), Invoices (22), Credit Memos (31) and Debit Memos (24) sections. Invoices: standalone create (single, batch of 50), correct (single, batch of 50), bulk post and unpost (the reversible pair), split a draft into 2 to 20, email, PDF status and files, items and taxation items, application parts, reverse (a credit memo is applied), write off (sync or async job), cancel (kept as Canceled; a job at 1,000 or more items) and delete (a job at 100 or more items). Memos: create (from charges, from an invoice, bulk of 50), correct, post and unpost, cancel (Draft only), delete (Cancelled only), email, PDF, files, items, taxation items; credit memos apply and unapply (also as async jobs), refund, reverse (a debit memo is created) and write off; debit memos collect, write off, due dates and application parts. Verified against the [Billing Documents](https://developer.zuora.com/v1-api-reference/api/billing-documents), [Invoices](https://developer.zuora.com/v1-api-reference/api/invoices), [Credit Memos](https://developer.zuora.com/v1-api-reference/api/credit-memos) and [Debit Memos](https://developer.zuora.com/v1-api-reference/api/debit-memos) references, version 2026-11-06. | Typed (build 5): 80 verbs plus `get_invoice`; `list_invoices` (legacy TSV export); object CRUD for `Invoice` | None in these four sections. Void is never called delete: cancel, reverse and write-off keep the document. Invoice schedules, taxation items as their own resource, e-invoicing and summary statements stay on `request` (build 6). |
| Payments and applications | HTTPS REST; `/v1/payments`, `/v1/payment-methods`, `/web-payments/sessions` | All 15 Payments operations: list (page-numbered, 16 filters, sort), create (external or electronic, optionally applied), read, parts and item parts, update, apply and unapply (the reversible pair), transfer, refund and refund-with-auto-unapply, cancel (terminal Canceled), delete (Canceled payments only). All 17 Payment Methods operations: create, create from an encrypted wallet token, read, update, verify, Plaid balance, scrub (terminal erase), delete (refused for the default), stored credential profiles (create, list, profile data, cancel and expire as the terminal pair), wallet domains (register, list, unregister), Payment Form sessions. Verified against the [Payments](https://developer.zuora.com/v1-api-reference/api/payments) and [Payment Methods](https://developer.zuora.com/v1-api-reference/api/payment-methods) references, version 2026-11-06. | Typed (build 4): 15 payment and 17 payment-method verbs; object CRUD for `Payment` | None in these two sections. Payment runs, payment schedules and refunds as their own resources stay on `request` (build 6). |
| Product catalog | HTTPS REST; `/commerce/products`, `/commerce/plans`, `/commerce/charges`, billable metrics/features and catalog query routes | Read, create, update, query, archive/delete where the catalog resource supports it. Exact resource-specific delete/retire semantics: **to verify against the Zuora v1 API reference**. | Object CRUD for `Product`; query/export | Add typed catalog verbs for products, plans, rate plans, charges, billable metrics, features, and catalog queries; include retire/restore if published. |
| Usage and usage ingestion | HTTPS REST; `/v1/usage` (file import), `/v1/object/usage`, `/v1/object/usage/{id}`, `/v1/invoices/invoice-item/{invoice-item-id}/usage-rate-detail` | All 6 operations in the vendor's Usage section: import a usage file (`.csv`, Excel or `.zip`; atomic, answered with a `checkImportStatus` URL), create one record, read it (`fields`; `X-Zuora-WSDL-Version` 146 or higher on import, create and read selects the product rate plan charge fields), correct it (Quantity, dates, UOM, RbeStatus), delete it (both refused for dynamic pricing usage charges), and read an invoice item's usage rate detail. The section publishes no usage list: Object Query (`query_objects` on `usages`) lists records. Verified against the [Usage reference](https://developer.zuora.com/v1-api-reference/api/usage), version 2026-11-06. | Typed (build 5): `import_usage`, `create_usage`, `get_usage`, `update_usage`, `delete_usage`, `get_usage_rate_detail` | None in this section. The Meters, Data Ingestion and Prepaid with Drawdown sections stay on `request` (build 6). |
| Bill runs | HTTPS REST; `/v1/bill-runs`, `/v1/bill-runs/{billRunId}` and its `/post`, `/cancel`, `/v1/bill-runs/{billRunKey}/emails`, `/v1/billing-preview-runs` | All 6 Bill Run operations: create (ad hoc or scheduled, by batch, bill cycle day or account filters), read (status Pending through Posted), post a Completed run (asynchronous, PostInProgress then Posted), cancel a Draft run (`cancelOnce` for one occurrence of a scheduled run, otherwise every future run), delete (Canceled or Error runs only), email its documents; both Billing Preview Run operations: create (forecast to `targetDate`) and read (status and `resultFileUrl`). Zuora publishes no rerun route. Verified against the [Bill Run](https://developer.zuora.com/v1-api-reference/api/bill-run) and [Billing Preview Run](https://developer.zuora.com/v1-api-reference/api/billing-preview-run) references, version 2026-11-06. | Typed (build 5): the 6 bill run and 2 billing preview run verbs | None in these sections. Bill Run Health stays on `request` (build 6). |
| Workflows | HTTPS REST; `/workflows`, `/workflows/{workflow_id}/export`, `/workflows/import`, versions, `/workflows/workflow_runs`, `/workflows/tasks`, `/workflows/metrics.json`, `/auth_providers` | All 20 operations in the vendor's Workflows section: list/get/update/delete definitions, export, import, version list/import/delete, run, run get/stop, task list/get/rerun/batch update, task usage, and callout auth provider create/update/delete. Zuora publishes no auth-provider read. Verified against the [Workflow API reference](https://developer.zuora.com/v1-api-reference/api/workflows/get_workflows), version 2026-11-06. | Typed (build 2): the 20 Workflow verbs in the current-surface table | None. Each verb takes the request headers its route documents (build 4). Workflow backup is `list_workflows` + `export_workflow` to `output_path`; restore is `import_workflow`/`import_workflow_version` from `input_path`. |
| Custom object definitions and records | HTTPS REST; `/objects/definitions/default`, `/objects/migrations`, `/objects/records/default/{object}`, `/objects/batch/default/{object}`, `/objects/jobs` | All 18 operations: definitions create, list, read, update (asynchronous migration answered with a `jobId`), delete (refused while records exist); records create (optional partial success), list (query, ids, cursor), read, replace (`If-Match` precondition), patch (JSON Merge Patch; `null` clears), delete, batch update or delete up to 1,000; bulk jobs submit, upload the one `.csv` (`text/csv`), list (cursor), read, row errors, cancel (terminal). Verified against the Custom Object [Definitions](https://developer.zuora.com/v1-api-reference/api/custom-object-definitions), [Records](https://developer.zuora.com/v1-api-reference/api/custom-object-records) and [Jobs](https://developer.zuora.com/v1-api-reference/api/custom-object-jobs) references, version 2026-11-06. | Typed (build 4): the 18 custom object verbs | None. |
| Object Query and ZOQL | HTTPS REST; `/object-query/{object}`, `/object-query/{object}/{key}`, `/v1/action/query`, `/v1/action/queryMore` | Object Query: 98 documented GET routes, a list and a by-key read for each standard object and for custom objects, with `filter[]` (EQ, NE, LT, GT, LE, GE, SW, IN; AND-combined), `expand[]`, one `sort[]`, `fields[]` and `<object>.fields[]`, `includeNullFields`, `pageSize` 1 to 99 and the `nextPage` cursor; the vendor publishes no write or cancel here. ZOQL: query and continuation. Verified against the [Object Queries reference](https://developer.zuora.com/v1-api-reference/api/object-queries), version 2026-11-06. | Typed (build 4): `query_objects`, `query_object`; `data_query`, `bulk_export` (ZOQL to TSV) | None. Query is not a substitute for mutation coverage; the mutation families keep their own rows. |
| SQL Data Query jobs | HTTPS REST; `/query/jobs`, `/query/jobs/{job-id}`, plus the job's pre-signed `dataFile` URL | Submit SQL job (JSON/CSV/TSV/DSV, NONE/GZIP/ZIP, `readDeleted` for the 30-day deleted-record window, LIVE or WAREHOUSE source), list/get job, cancel an accepted or in-progress job (terminal `cancelled`), and download the result. Verified against the [Data Queries reference](https://developer.zuora.com/v1-api-reference/api/data-queries), version 2026-11-06. | Typed (build 2): `submit_data_query`, `list_data_query_jobs`, `get_data_query_job`, `cancel_data_query_job`, `download_data_query_result` | None. Zuora documents no continuation token for the job list; `page_size` sizes the one response, and `list_data_query_jobs` reports `may_have_more` (the response filled `page_size`) with a hint to filter or raise `page_size` (build 3). |
| Settings, notifications, email templates, and tenant configuration | HTTPS REST; `/settings/listing`, `/settings/batch-requests`, notification/template/configuration routes | Read and mutate tenant settings, notifications, email templates, and configuration. Exact route families and versioned payloads: **to verify against the Zuora v1 API reference**. | None typed; generic query/export cannot safely mutate configuration | Add typed settings/configuration verbs after route inventory; use `request` for the long tail. Every mutable setting needs get/set/correct/clear and audit readback. |
| Files, exports, and bulk jobs | HTTPS REST; `/v1/files/{file-id}`, `/v1/files/{file-id}/status`, `/v1/files/{file-id}/restore`, plus export/bulk-data job routes | Files: download, status and restore are verified against the [Files reference](https://developer.zuora.com/v1-api-reference/api/files/get_files), version 2026-11-06; Zuora publishes no file delete. Export/bulk-data job create/poll/cancel routes: **to verify against the Zuora v1 API reference**. | Typed (build 2): `download_file`, `get_file_status`, `restore_file` (with the documented v1 headers since build 4); `bulk_export` writes ZOQL results to a local TSV; `request` covers export-job routes | Add typed export-job lifecycle verbs after route verification (build 6: the vendor's Bulk Data section, 8 operations). The local TSV is an output policy, not vendor API coverage. |
| Generic authenticated request (long tail) | HTTPS REST; any relative path under the registered tenant base URL | Caller supplies method, relative path, query, headers, and JSON/body or download mode; supports vendor reads, writes, deletes, and newly published families subject only to vendor RBAC. | `request` (build 1) | None. Typed verbs remain preferred for stable high-value families. |

No row above is “unsupported.” “None typed” means the generic path or the
typed build unit is the coverage route. A route that cannot be confirmed during
implementation must retain the explicit “to verify” label until the unit
captures the vendor reference or a live tenant observation.

## Lifecycle and authorization requirements

Every typed family must inventory these operations before it is considered
complete:

1. create and current read, including historical readback where the vendor
   exposes it;
2. update/correct and clear/retire, including mutable optional fields;
3. close/reopen, cancel/terminate, reverse/void, or another vendor-defined
   terminal transition when applicable;
4. delete where the vendor publishes a delete operation, with the vendor's
   preconditions and RBAC error preserved; and
5. stale-write/concurrency behavior, authorization premise, append-only audit
   history, and tests for correction, stale refusal, reversal, and terminal
   transitions.

The generic `request` verb does not weaken those requirements. It supplies
coverage while a family is awaiting a typed wrapper, and it must not silently
retry non-idempotent writes. An OAuth client lacking a permission receives the
vendor's authorization failure; the plugin does not relabel that as
“unsupported.”

## Read-only posture sites to remove or revise

These were the posture statements that conflicted with the feature request's
vendor-RBAC-only boundary. Build 2 revised every site inside the plugin.

| Path | Former statement | Disposition (build 2) |
| --- | --- | --- |
| `plugins/zuora_plugin/plugin.yaml` (description) | Typed billing actions have no delete verb. | Revised: lists the typed Workflow/Data Query/Files surface; reads, writes and deletes first-class. |
| `plugins/zuora_plugin/plugin.yaml` (description, final paragraph) | No delete verb; create/update cover writes. | Revised: vendor RBAC is the only boundary; void/cancel keep vendor semantics. |
| `plugins/zuora_plugin/knowledge_base/01_zuora_overview.md` (“Vendor-controlled access”) | Typed billing actions have no delete verb. | Revised: current lifecycle inventory; void/cancel distinguished from delete. |
| `plugins/zuora_plugin/knowledge_base/hydration_guidance.md` (embedding description) | Create/update but never delete. | Revised. |
| `plugins/zuora_plugin/knowledge_base/hydration_guidance.md` (pitch) | No delete; structurally cannot delete. | Revised: the vendor role decides what is allowed. |
| `plugins/zuora_plugin/src/zuora_plugin/billing_actions.py` (module docstring) | No delete verb exists. | Revised: deletes go through the typed verbs or `request`. |
| `plugins/zuora_plugin/src/zuora_plugin/plugin.py` (module docstring) | Object CRUD/billing reads and no delete. | Revised: lists the typed surface and the vendor-RBAC boundary. |
| `plugins/zuora_plugin/src/zuora_plugin/constants.py` (`SUPPORTED_OBJECT_TYPES`) | Object types restricted to five. | Kept as typed-verb input validation only; `request` does not inherit it. |
| Full plugin roster article in the platform knowledge base (plugin inventory section) | “No delete verb.” | Revised by build 3: the `zuora_plugin` row names the typed Workflow, Data Query, Files, Orders, Subscriptions and Accounts verbs with their deletes, and vendor RBAC as the only boundary. |

Build 5 added no posture site: every new billing document, memo, bill run and
usage verb, including its deletes, cancels, reversals and write-offs, is
first-class and bounded only by the vendor role. The roster row above still
names only the build 1 to 3 families (no Payment Methods, Payments, Custom
Objects, Object Query, billing documents, memos, bill runs or usage); updating
it is a follow-up recorded on `unt_37fa3a6a-658a-476b-adad-034792b43e1b`,
because queued landings own that file.

## Proposed build order

The order keeps writes and deletes first-class and makes the Workflow backup
usable early. Sizes are rough per unit and include lifecycle tests.

| Order | Unit | Deliverable | Size |
| ---: | --- | --- | :---: |
| 1 | Generic authenticated request | Done (build 1, `unt_6f321bd5-a790-48c0-a0d6-8c420cf2eac5`): one safe relative-path method for the long tail; common response/error/audit contract | M |
| 2 | Orders and subscriptions | Done (build 3, `unt_8760e024-8444-4d5f-881e-e938e1a96f76`): every Orders and Subscriptions operation | L |
| 3 | Accounts and payment methods | Done: Accounts (build 3, same unit) including the account's payment-method list, default and cascading; the `/v1/payment-methods` lifecycle in build 4 (unit 5) | M |
| 4 | Invoices, billing documents, memos | Done (build 5, `unt_37fa3a6a-658a-476b-adad-034792b43e1b`): all 81 Billing Documents, Invoices, Credit Memos and Debit Memos operations | L |
| 5 | Payments and applications | Done (build 4, `unt_7ab0db00-581e-4d89-8401-1f9c318f14df`): all 15 Payments and all 17 Payment Methods operations | L |
| 6 | Workflows and files | Done (build 2, `unt_8ecd1cf2-43a5-4660-8171-a670fab6aa81`): backup export/download, import, versions, runs, tasks, usage, auth providers, file download/status/restore | L |
| 7 | Custom objects | Done (build 4, same unit): definitions, records, batch, bulk jobs with CSV upload, migration, delete and job cancellation | L |
| 8 | Catalog | Products, plans, rate plans, charges, metrics/features, retire/restore | L |
| 9 | Usage and bill runs | Done (build 5, same unit): all 6 Usage, 6 Bill Run and 2 Billing Preview Run operations; Zuora publishes no bill-run rerun | L |
| 10 | Queries | SQL Data Query jobs/cancel/download done in build 2; Object Query done in build 4; remaining: ZOQL refinements, AQuA if required | L |
| 11 | Settings and tenant configuration | Settings, notifications, templates, exports, configuration/version inventory | M |

Build 5 delivered units 4 and 9 and the documented request headers on the
legacy `list_subscriptions` and `get_invoice` verbs. Build 6 is next: the
product catalog including catalog groups (unit 8: the vendor's Commerce,
Products, Product Rate Plans, Product Rate Plan Charges, Product Charge
Definitions and Catalog Groups sections), settings, notifications and export
jobs (unit 11: Settings, Notifications, Bulk Data and Configuration
Templates), and the billing-adjacent sections still on `request` (Invoice
Schedules, Taxation Items, Payment Runs, Payment Schedules, Refunds, Meters).

Each unit must update its typed process JSON, preserve current async completion
semantics where appropriate, add correction/stale-write/reversal/terminal tests,
and update the posture sites listed above only when the implementation exists.

## Acceptance mapping

| Feature-request acceptance clause | How this inventory maps it |
| --- | --- |
| Workflow backup runs end to end through the plugin with no local script holding the client secret | Builds 1 and 2 provide authenticated Workflow list/export/import (typed, with `.json` backup files under `export_allowed_roots`) through the registered `zuora_tenant` OAuth client; the client secret remains address-book/vault resolved and never enters a local script. |
| A vendor-API-coverage table lists every Zuora REST family with its verb or generic path | The family table covers accounts, subscriptions, orders, invoices, payments, catalog, usage, bill runs, workflows, custom objects, Object Query, ZOQL, SQL Data Query, settings/configuration, files/exports, and the single generic `request` path. |
| Nothing is marked unsupported | Every row has current coverage, a typed build unit, or `request`; uncertain endpoints are explicitly “to verify,” never unsupported. |
| Vendor RBAC is the only boundary, including writes and deletes | Lifecycle requirements and the generic verb delegate authorization to the registered OAuth client's Zuora role; plugin-side read-only/no-delete statements are identified for removal. |
| The seed issue is answered with the release that ships it | Release close-out must cite the candidate/release containing the typed units and generic path, then link the release back to seed issue #95. |

## References

- [Zuora v1 API reference](https://developer.zuora.com/v1-api-reference/api)
- [Object Queries](https://developer.zuora.com/v1-api-reference/api/object-queries)
- [Data Queries](https://developer.zuora.com/v1-api-reference/api/data-queries)
- [Custom Object Records](https://developer.zuora.com/v1-api-reference/api/custom-object-records/get_allrecordsforcustomobjecttype)
- [Workflow API](https://developer.zuora.com/v1-api-reference/api/workflows/get_workflows)
- [Bill Run API](https://developer.zuora.com/v1-api-reference/api/bill-run)
- [Payment Runs](https://developer.zuora.com/v1-api-reference/api/payment-runs)
- [Files](https://developer.zuora.com/v1-api-reference/api/files/get_files)
