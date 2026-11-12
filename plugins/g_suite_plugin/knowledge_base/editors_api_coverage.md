# Docs, Sheets and Slides API coverage

Build 4 adds 25 typed operations: all 3 Docs v1 methods, all 17 Sheets v4
methods and all 5 Slides v1 methods in Google's discovery revision 20261106,
read from the live discovery documents on 2026-11-07. Each has its own process,
typed named path and query arguments, an EDGE definition and a process
knowledge entry. The older convenience verbs (`docs_create`, `docs_get`,
`docs_batch_update`, `sheets_create`, `sheets_get_values`, `sheets_update_values`,
`sheets_append_values`, `sheets_batch_update`, `slides_create`, `slides_get`,
`slides_batch_update`) remain unchanged. Use the complete operations below when
you need a vendor parameter, body field or response field those verbs omit.

Forms is a separate, later build in the Google programme order, so it is not
covered here. Every Docs, Sheets and Slides method in the discovery documents
is covered, and none was left out.

The vendor contracts are packaged in the plugin's `discovery/` resources, so an
older installed Google client builds the current methods and parameters (for
example `includeTabsContent`, `commentsViewMode` and
`excludeTablesInBandedRanges`). No additional dependency is needed.

Sources:

- [Docs v1 REST reference](https://developers.google.com/workspace/docs/api/reference/rest)
  and [discovery document](https://docs.googleapis.com/$discovery/rest?version=v1).
- [Sheets v4 REST reference](https://developers.google.com/workspace/sheets/api/reference/rest)
  and [discovery document](https://sheets.googleapis.com/$discovery/rest?version=v4).
- [Slides v1 REST reference](https://developers.google.com/workspace/slides/api/reference/rest)
  and [discovery document](https://slides.googleapis.com/$discovery/rest?version=v1).

## Accounts, scopes and errors

Every operation takes the optional `account` argument: an email address or an
alias. The account store selects the named account, or the default one, and the
completion names the account that answered. An unknown selector returns
`gsuite.unknown_account` at dispatch, listing the connected accounts. Docs,
Sheets and Slides exist for personal `@gmail.com` accounts as well as
Workspace accounts, so no account tier is refused. Vendor role-based access is
the only boundary: Google decides what each account may read or edit.

No consent scope is added. The connector already requests `documents`,
`spreadsheets`, `presentations` and `drive`, and every method accepts at least
one of them. Read methods also accept the narrower read-only grants listed in
the tables below. If an account's recorded grant is known and lacks every
accepted scope, the call returns `gsuite.missing_scope` before it reaches
Google. The message names the acceptable scopes and the reconnect steps,
`start_interface` then `connect_account`. Unknown older grant records are
advisory, and the request goes to Google.

Google 400/401/403/404/429 errors keep their vendor message through the shared
taxonomy: `gsuite.invalid_params`, `gsuite.auth_expired`,
`gsuite.permission_denied` or `gsuite.missing_scope`, `gsuite.not_found` and
`gsuite.rate_limited`. Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values. Raw `alt`/`callback` replies are
written to blob storage as received.

## Parameters

Parameter names match the vendor's, including camelCase and dotted names such
as `thumbnailProperties.thumbnailSize`. Repeated parameters such as `ranges`
take arrays. Each operation also accepts every API-wide parameter: `fields`,
`prettyPrint`, `quotaUser`, `$.xgafv`, `alt`, `callback`, `uploadType` and
`upload_protocol`. `fields` defaults to `*`, so no response field is silently
dropped. The official client always rewrites `alt` to `json` and cannot parse
a JSONP reply. So with `alt: "proto"`, `alt: "media"` or a `callback`, the
connector sends your value and stores Google's raw reply as a blob. The
completion returns `file_blob_key`, `mime_type` and `size` in place of
`response`. Google decides whether it serves that format: on 2026-11-07 Docs
answered `alt=proto` with 400 "Proto over HTTP is not allowed", and that vendor
error comes back unchanged.

`key`, `access_token` and `oauth_token` are refused with `gsuite.invalid_params`
before anything is queued. The connected account authenticates every call.
The refusal names the parameter and never repeats its value, whatever its
length.

## Request bodies

JSON request content goes in `body` under the vendor's field names. Before the
job is queued, the body is checked against the method's request schema in the
pinned discovery document, and then passed through unchanged:

- every field must be a declared field of its schema;
- each value must have the declared JSON type and enum value, and int32 and
  uint32 numbers must be in range;
- each entry of a `batchUpdate` `requests` array (the `Request` union) must
  set exactly one request kind, because Google applies one kind per entry.

JSON `null` clears an optional field. Google still validates semantics such as
indexes, object ids and field masks. The error names the body path and the rule
it broke, for example `body.requests[0].insertText.location.index must be an
integer (int32)`.

## Lifecycle and concurrency

All mutations run with the selected account's authorization, as background
jobs. GETs are retryable, writes are not, and no stale write is retried
automatically.

- Documents: create with `docs_documents_create` (Google uses only `title`).
  Read with `docs_documents_get`. Correct and clear content through
  `docs_documents_batch_update` request kinds: insert, replace, update style,
  and delete content, named ranges, tabs, headers and footers. For
  optimistic concurrency, `writeControl.requiredRevisionId` makes Google refuse
  a write against a stale revision, and the vendor error is preserved.
  `targetRevisionId` merges instead.
- Spreadsheets: create, read (`get`, `getByDataFilter`), and value
  write/append/read/clear, including the batch and data-filter forms. Sheets,
  ranges, formats and developer metadata are added, corrected and deleted
  through `sheets_spreadsheets_batch_update`. Developer metadata reads are
  `developer_metadata_get` and `_search`. Copy a sheet to another spreadsheet
  with `sheets_spreadsheets_sheets_copy_to`. Sheets publishes no revision
  precondition, so the last write wins. Read before correcting.
- Presentations: create, read, page read, page thumbnail, and every object
  create, update or delete request kind through
  `slides_presentations_batch_update`, with the same `writeControl` revision
  check as Docs.
- Terminal and history: the editor APIs have no delete method. Trash or
  restore a file with `drive_files_update` (`body.trashed`), delete it
  permanently with `drive_files_delete`, and read revision history with
  `drive_revisions_list`. These are all Build 3 Drive operations.

## Methods

### Docs v1

| Process | Vendor method | HTTP | Body | Accepted scopes |
|---|---|---|---|---|
| `docs_documents_get` | `docs.documents.get` | `GET v1/documents/{documentId}` | none | documents, documents.readonly, drive, drive.file, drive.readonly |
| `docs_documents_batch_update` | `docs.documents.batchUpdate` | `POST v1/documents/{documentId}:batchUpdate` | BatchUpdateDocumentRequest | documents, drive, drive.file |
| `docs_documents_create` | `docs.documents.create` | `POST v1/documents` | Document | documents, drive, drive.file |

### Sheets v4 spreadsheets, sheets and developer metadata

| Process | Vendor method | HTTP | Body | Accepted scopes |
|---|---|---|---|---|
| `sheets_spreadsheets_get_by_data_filter` | `sheets.spreadsheets.getByDataFilter` | `POST v4/spreadsheets/{spreadsheetId}:getByDataFilter` | GetSpreadsheetByDataFilterRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_batch_update` | `sheets.spreadsheets.batchUpdate` | `POST v4/spreadsheets/{spreadsheetId}:batchUpdate` | BatchUpdateSpreadsheetRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_get` | `sheets.spreadsheets.get` | `GET v4/spreadsheets/{spreadsheetId}` | none | drive, drive.file, drive.readonly, spreadsheets, spreadsheets.readonly |
| `sheets_spreadsheets_create` | `sheets.spreadsheets.create` | `POST v4/spreadsheets` | Spreadsheet | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_developer_metadata_get` | `sheets.spreadsheets.developerMetadata.get` | `GET v4/spreadsheets/{spreadsheetId}/developerMetadata/{metadataId}` | none | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_developer_metadata_search` | `sheets.spreadsheets.developerMetadata.search` | `POST v4/spreadsheets/{spreadsheetId}/developerMetadata:search` | SearchDeveloperMetadataRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_sheets_copy_to` | `sheets.spreadsheets.sheets.copyTo` | `POST v4/spreadsheets/{spreadsheetId}/sheets/{sheetId}:copyTo` | CopySheetToAnotherSpreadsheetRequest | drive, drive.file, spreadsheets |

### Sheets v4 values

| Process | Vendor method | HTTP | Body | Accepted scopes |
|---|---|---|---|---|
| `sheets_spreadsheets_values_clear` | `sheets.spreadsheets.values.clear` | `POST v4/spreadsheets/{spreadsheetId}/values/{range}:clear` | ClearValuesRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_values_append` | `sheets.spreadsheets.values.append` | `POST v4/spreadsheets/{spreadsheetId}/values/{range}:append` | ValueRange | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_values_update` | `sheets.spreadsheets.values.update` | `PUT v4/spreadsheets/{spreadsheetId}/values/{range}` | ValueRange | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_values_batch_get_by_data_filter` | `sheets.spreadsheets.values.batchGetByDataFilter` | `POST v4/spreadsheets/{spreadsheetId}/values:batchGetByDataFilter` | BatchGetValuesByDataFilterRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_values_get` | `sheets.spreadsheets.values.get` | `GET v4/spreadsheets/{spreadsheetId}/values/{range}` | none | drive, drive.file, drive.readonly, spreadsheets, spreadsheets.readonly |
| `sheets_spreadsheets_values_batch_clear` | `sheets.spreadsheets.values.batchClear` | `POST v4/spreadsheets/{spreadsheetId}/values:batchClear` | BatchClearValuesRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_values_batch_get` | `sheets.spreadsheets.values.batchGet` | `GET v4/spreadsheets/{spreadsheetId}/values:batchGet` | none | drive, drive.file, drive.readonly, spreadsheets, spreadsheets.readonly |
| `sheets_spreadsheets_values_batch_update` | `sheets.spreadsheets.values.batchUpdate` | `POST v4/spreadsheets/{spreadsheetId}/values:batchUpdate` | BatchUpdateValuesRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_values_batch_update_by_data_filter` | `sheets.spreadsheets.values.batchUpdateByDataFilter` | `POST v4/spreadsheets/{spreadsheetId}/values:batchUpdateByDataFilter` | BatchUpdateValuesByDataFilterRequest | drive, drive.file, spreadsheets |
| `sheets_spreadsheets_values_batch_clear_by_data_filter` | `sheets.spreadsheets.values.batchClearByDataFilter` | `POST v4/spreadsheets/{spreadsheetId}/values:batchClearByDataFilter` | BatchClearValuesByDataFilterRequest | drive, drive.file, spreadsheets |

### Slides v1

| Process | Vendor method | HTTP | Body | Accepted scopes |
|---|---|---|---|---|
| `slides_presentations_batch_update` | `slides.presentations.batchUpdate` | `POST v1/presentations/{presentationId}:batchUpdate` | BatchUpdatePresentationRequest | drive, drive.file, drive.readonly, presentations, spreadsheets, spreadsheets.readonly |
| `slides_presentations_create` | `slides.presentations.create` | `POST v1/presentations` | Presentation | drive, drive.file, presentations |
| `slides_presentations_get` | `slides.presentations.get` | `GET v1/presentations/{+presentationId}` | none | drive, drive.file, drive.readonly, presentations, presentations.readonly |
| `slides_presentations_pages_get` | `slides.presentations.pages.get` | `GET v1/presentations/{presentationId}/pages/{pageObjectId}` | none | drive, drive.file, drive.readonly, presentations, presentations.readonly |
| `slides_presentations_pages_get_thumbnail` | `slides.presentations.pages.getThumbnail` | `GET v1/presentations/{presentationId}/pages/{pageObjectId}/thumbnail` | none | drive, drive.file, drive.readonly, presentations, presentations.readonly |
