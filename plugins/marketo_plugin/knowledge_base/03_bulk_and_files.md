# Marketo Files and Bulk Transfers

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:execution, evidence-category:operations-runbook, domain:marketo, domain:bulk-transfer

Embedding Description: Typed Marketo file and image multipart uploads, lead/custom-object/program-member imports and lead/activity/program-member extracts. Explains every caller parameter, vendor job submission, status, row reports, cancellation, downloads, continuation and the separate platform job ledger.

## File and image operations

All operations return a platform `job_id` immediately. Poll
`check_marketo_job_status` for the result. Vendor RBAC and workspace permissions
authorize each call; setup's read probes do not establish write permissions.

| Verb | Parameters | Finished result |
|---|---|---|
| `get_asset_file` | `file_id`, `output_tsv_path` | Metadata TSV |
| `get_asset_file_by_name` | `name`, `output_tsv_path` | Metadata TSV |
| `list_asset_files` | `output_tsv_path`; optional `folder`, `offset`, `max_return`, `row_limit`, `acknowledge_default_limit_override` | Internally paged metadata TSV; `next_offset` if truncated |
| `create_asset_file` | `name`, `folder`, `file`; optional `description`, `insert_only` | Vendor envelope with file id, URL, MIME type and size |
| `update_asset_file_content` | `file_id`, `file` | Vendor envelope after content replacement |

`folder` is an object with numeric `id` and `type` (`Folder` or `Program`).
`file` is a JSON-safe part object:

```json
{"filename":"example.csv","content_type":"text/csv","content_base64":"Y29sdW1uCg=="}
```

The worker decodes the bytes and HTTPX constructs multipart/form-data with its
own boundary. The same interface uploads images with the appropriate filename
and MIME type. It does not read arbitrary local file paths. For generic
requests, put parts under `multipart_files`, keyed by the vendor form field
name, and other fields in `form_body`. Do not combine these with `json_body`.
Caller Content-Type is replaced by HTTPX's correct multipart boundary.

File creation upserts by name unless `insert_only=true` requests vendor
collision refusal. An empty description clears it on upsert. Replace content
to correct it; restore saved content to reverse that replacement. Adobe
publishes no Files delete, archive, move, approve or reopen endpoint in the
verified Files reference. These verbs do not invent those actions.

File metadata reads require an absolute `.tsv` path under configured
`export_allowed_roots` with an existing parent. Browse pages at `max_return`
(1–200, default 200), starting at `offset` (default 0), until completion or the
total 500-row default limit. Override with `row_limit` (maximum 5,000) and
`acknowledge_default_limit_override=true`. Continue from `next_offset` with the
same filters if truncated; a limit cutting a page never skips its remainder.

## Bulk Import

| Submission verb | Required | Optional |
|---|---|---|
| `import_leads` | `file`, `format` | `lookup_field`, `partition_name`, `list_id` |
| `import_custom_objects` | `api_name`, `file`, `format` | None |
| `import_program_members` | numeric string `program_id`, `program_member_status`, `file`, `format` | None |

`file` has the part shape above; `format` is csv, tsv or ssv (program members
also documents uppercase CSV/TSV/SSV). Supply a header row naming REST fields.
Vendor limits apply: imports are smaller than 10 MB, at most two process
concurrently and ten can be queued including processing jobs. The vendor
validates rows, field names, permissions, formats and subscription limits.
Lead `lookup_field` defaults to email; `id` selects update-only. `list_id`
adds imported leads to that static list; `partition_name` chooses the partition.

Submission uploads and automatically queues the import. The result carries
vendor `batchId` and status, rather than individual row successes. Retain that
id and use `get_<family>_import_status`, `get_<family>_import_failures`, and
`get_<family>_import_warnings`, with family `lead`, `custom_object` or
`program_member`. All take integer `batch_id`; custom-object operations also
take `api_name`. Program-member status/report operations do not take program_id.

Poll vendor status every 5–30 seconds, issuing a fresh platform job for each
operation. `Queued` and `Importing` are pending; `Complete` and `Failed` are
terminal. `Complete` can still carry nonzero `numOfRowsFailed` or
`numOfRowsWithWarning`; retrieve and report both files rather than declaring
every row successful. Report bodies retain original delimiters and reasons.
Vendor batch ids are retained for seven days. No import cancel/delete/reopen
endpoint or bulk activity-import API is published in the verified reference.
Correct imported values through another import or the ordinary record verbs;
clear mutable fields through the appropriate vendor operation. A submitted
import cannot be locally undone by deleting its platform job observation.

## Bulk Extract

For each family `lead`, `activity`, and `program_member`, the typed verbs are:

| Verb pattern | Parameters | Vendor operation |
|---|---|---|
| `list_bulk_<family>_extract_jobs` | optional `status` array, `batch_size` (1–300), `next_page_token` | One vendor page; preserve `nextPageToken` and continue with next_page_token |
| `create_bulk_<family>_extract` | `filter`, `fields` (optional for activity); optional `format`, `column_header_names` | Define a new vendor job, not yet enqueued |
| `enqueue_bulk_<family>_extract` | `export_id` | Queue the created job |
| `get_bulk_<family>_extract_status` | `export_id` | Read vendor status and counts |
| `download_bulk_<family>_extract` | `export_id`; optional `range` | Download completed export bytes |
| `cancel_bulk_<family>_extract` | `export_id` | Cancel the vendor job where its current state permits |

`fields` is an array of REST field names. `format` is CSV, TSV or SSV (default
CSV). `column_header_names` maps field names to exported column names. The
entire vendor filter object is forwarded, including all nested options:

* Leads: one of createdAt, updatedAt, staticListName, staticListId,
  smartListName, smartListId. Date filters contain startAt/endAt.
* Activities: createdAt plus optional activityTypeIds and either
  primaryAttributeValueIds or primaryAttributeValues. Primary-attribute
  filters need compatible activity types; the vendor validates combinations.
* Program members: programId or programIds; optional isExhausted,
  nurtureCadence, statusNames and updatedAt. The current guide adds these
  options beyond the older OpenAPI filter description. Date ranges must be
  31 days or less; some filters require subscription infrastructure.

Lists accept the vendor status values created, queued, processing, cancelled,
completed and failed. Their `nextPageToken` remains visible, so one page never
claims a complete enumeration. Only the API user that owns an extract can
inspect its vendor status. Bulk permissions and subscription quotas are
enforced by Marketo, without an additional plugin role or confirmation gate.

Retain `exportId` from create, enqueue it, then poll until `Completed`, `Failed`
or `Canceled`. A completed platform job confirms one HTTP operation; it does
not promote vendor `Created`, `Queued`, `Processing` or `Failed` to successful
extraction. Each platform handle preserves the observation made by that call.
An extract cannot be edited or reopened through these published endpoints;
cancel when permitted and create a new job with corrected parameters instead.

Downloads return HTTP `status`, the text `body`, the byte content as
`body_base64`, and `content_range`. `range` is an HTTP byte range such as
`bytes=0-1023`. Use body_base64 for byte fragments, including fragments cutting
a UTF-8 character. They remain in the job result; no TSV file is invented.
Range values are forwarded after header-injection validation, including the
multiple-range whitespace, suffix ranges and case-insensitive units documented
by [HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-14.1.2).
Generic request downloads use the same response fields.

## Authorization, history and concurrent changes

These endpoints publish no expected-version, ETag or conditional-write
parameter. File writes and correcting imports are last-write-wins. The
plugin cannot promise stale-write refusal that the vendor does not provide;
serialize changes and read back before retrying an uncertain mutation.
`insert_only` is the vendor's create-collision guard, not an update CAS.
Only explicit invalid/expired-token envelopes cause one auth refresh/retry.
Transport failures and permission refusals never trigger mutation retries.

Current state is read through typed metadata/status verbs. Historical results
remain in each platform job handle; no vendor revision log or local bulk
state machine is fabricated. Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.
Vendor control keys, paging tokens, error codes and outcome flags are read
from the response itself.

Endpoint verification and no-API evidence are linked in
`vendor_api_coverage.md`. Build 6 retains smart/static list objects and rules,
Programs, Smart Campaigns, email/templates, landing pages, forms, tags/channels,
schema, and custom-object Bulk Extract. The generic request reaches these
remaining families under the same registered origin and vendor RBAC.

### Native values

Outward vendor values and dictionary keys are normalized to JSON-safe forms:
binary values decode as UTF-8 with replacement, tuples become JSON arrays,
Decimal values become decimal text and dates/times use ISO text. Platform job
handles, status enums and HTTP status/correlation fields remain native.
