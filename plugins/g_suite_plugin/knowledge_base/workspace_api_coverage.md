# Drive, People and Tasks API coverage

Build 3 adds 102 typed operations: all 64 Drive v3 methods, all 24 People v1
methods and all 14 Tasks v1 methods in Google's discovery revision 20261106.
Each has its own process, typed named query/path arguments, EDGE definition and
process knowledge entry. Existing convenience Drive verbs remain available.
Use the complete `drive_files_list` operation when you need arbitrary metadata,
shared-drive parameters, search, or pagination beyond the convenience listing.

The vendor contracts are pinned in the plugin's packaged `discovery/` resources.
They include schemas and media definitions, so older installed Google client
versions can build newer approvals and access-proposal operations. Builders
receive isolated copies because the official client mutates discovery objects.
No additional dependency is needed.

Sources:

- [Drive v3 REST reference](https://developers.google.com/workspace/drive/api/reference/rest/v3)
  and [discovery document](https://www.googleapis.com/discovery/v1/apis/drive/v3/rest).
- [People v1 REST reference](https://developers.google.com/people/api/rest)
  and [discovery document](https://people.googleapis.com/$discovery/rest?version=v1).
- [Tasks v1 REST reference](https://developers.google.com/workspace/tasks/reference/rest)
  and [discovery document](https://www.googleapis.com/discovery/v1/apis/tasks/v1/rest).

## Accounts, scopes and errors

Every operation takes optional `account` (email or alias). The account store
selects the named account or configured default, and the completion names that
account. An unknown selector produces `gsuite.unknown_account`. Tokens stay in
that account's vault pair; authentication-query overrides are not exposed.

All operations run as background jobs. Dispatch returns `job_id` and `status`;
the completion contains `account`, `method` and `response`, preserving the
vendor's complete response envelope. `response_headers` retains ETag and
Last-Modified when Google provides them for conditional requests. Terminal operations with no response
schema return `response: null`; methods with an Empty schema may return `{}`.
Media downloads and exports return `file_blob_key`, `mime_type` and byte `size`.
Upload-capable operations accept `blob_key` and optional `mime_type`; ingest a
local file through blob storage first. Request content never reads arbitrary
local paths. Download, metadata read and long-running Drive download are
separate vendor methods: `drive_files_download` returns an operation; poll it
with `drive_operations_get`, then use the vendor's resulting download URL.

New consent scopes are `drive.apps.readonly`, `contacts`,
`contacts.other.readonly`, `directory.readonly`, `tasks`,
`user.addresses.read`, `user.birthday.read`, `user.emails.read`,
`user.gender.read`, `user.organization.read`, `user.phonenumbers.read`,
`userinfo.email` and `userinfo.profile`, all under
`https://www.googleapis.com/auth/`. Existing Drive consent is retained.
Separate profile-field scopes are required to read those profile fields;
Contacts consent alone does not grant them. Read verbs accept the narrower
read-only grants in the vendor's per-method scope list. No redundant Contacts
or Tasks read-only scope is added to write consent. The table below lists each
method's accepted scopes. The setup contract is advanced separately; these
scope additions must be included in that follow-up.

Existing accounts must reconnect with `start_interface` and `connect_account`
and approve the added scopes. Known missing grants return
`gsuite.missing_scope` naming acceptable scopes and reconnection. Unknown old
scope records remain advisory: the request reaches Google. A credential's
recorded grants and tier remain owned by the account contract. Personal
Google accounts can use contacts and tasks. The two People directory methods
require a Workspace domain; a recorded `consumer` tier gets one message naming
that tier and the option to select a Workspace account. Unknown tiers reach
Google. Drive features can depend on vendor subscription/administrator
entitlements; Google supplies the reason for those denials.

Google 400/401/403/404/429 errors retain their reason and message through the
shared taxonomy. `gsuite.auth_expired` instructs reconnection; refresh-token
`invalid_grant` explains the seven-day expiry for External OAuth apps in Testing
mode. Secret redaction covers raw, URL-encoded and base64 variants in errors
and successful responses. No extra confirmation or read-only mode is added.

## Calling conventions and paging

Parameter names match the vendor, including camelCase: `fileId`, `resourceName`,
`pageToken`, `personFields`, `updatePersonFields`, `readMask`, `tasklist`, `task`.
Repeated parameters take arrays. JSON request content goes in `body`, preserving
vendor field names. The table names each request schema; its complete fields,
read-only annotations, masks and etag rules are in the linked REST reference
and packaged discovery document. Vendor type, required-parameter, enum, pattern
and numeric range validation runs before transport; Google validates nested
body semantics and entitlement rules. Unknown fields are not silently removed.

Calls perform one page. Return `response.nextPageToken` as `pageToken` to
continue; preserve `nextSyncToken`, `newStartPageToken` and deletion tombstones
for incremental reads. A `fields` mask can narrow the response; it defaults to
`*` so metadata and continuation tokens are not silently discarded. Include
paging fields if supplying your own mask. Page-size defaults and maxima are
Google's published ones; no new local row cap is imposed.

Examples:

- Search Drive: `drive_files_list` with `q: "trashed = false"`,
  `supportsAllDrives: true`, `pageSize: 1000`; continue with `pageToken`.
- Correct metadata: `drive_files_update`, `fileId`,
  `body: {"description": "Corrected description"}`. Trash and restore by setting
  `body.trashed` true and false. Permanent deletion is `drive_files_delete`.
- Contacts: `people_people_connections_list`, `resourceName: "people/me"`,
  `personFields: "names,emailAddresses"`; edit with
  `people_people_update_contact`, the current Person `etag`, and
  `updatePersonFields: "names"`. Clear a mutable field by including it in the
  update mask and supplying its empty value as documented by Google.
- Tasks: `tasks_tasks_insert`, `tasklist: "@default"`,
  `body: {"title": "Follow up"}`. Complete using `tasks_tasks_patch` and
  `body.status: "completed"`; reopen using `"needsAction"` and clear `completed`.

## Lifecycle and concurrency

All mutations use the selected account's OAuth authorization. No local audit
or alternate database is introduced: vendor histories and revision semantics
remain authoritative. Optional `if_match` and `if_none_match` set conditional
HTTP ETag headers; Google enforces them where the particular method supports
conditional requests. Read the current object before correcting it. People
contact correction also requires its current body `etag` and field masks;
Google refuses stale etags. A vendor refusal is preserved; the plugin never
retries a stale write automatically. GETs are marked retryable, writes are not.

- Drive files: create/read/update, reversible trash/restore, terminal delete,
  revisions list/get/update/delete, labels list/modify, copy/export, changes
  and watches. Permissions, comments and replies have full create/read/update/
  delete operations. Deleted comments/replies use the vendor's tombstone rules;
  no unsupported undelete is invented. Shared drives have create/read/update/
  delete and hide/unhide; deprecated teamdrives aliases remain exposed because
  the vendor still publishes them. Channels stop an existing watch. Approval
  start/read/approve/decline/cancel/reassign/comment and access-proposal
  resolution use the published request schemas. Application metadata, about
  and operation status are vendor read surfaces, with no invented write verb.
- People: contact create/read/update/delete, batch operations, set/delete photo;
  group CRUD and membership add/remove. Other contacts support list/search/copy
  into contacts; the vendor publishes no other-contact update/delete methods.
  Directory and profile reads have no mutation operation in People v1.
- Tasks: task-list and task CRUD, patch/full update, move, completion/reopening,
  deletion and clear-completed. List with `showCompleted`, `showHidden` and
  `showDeleted` as needed for current/tombstone readback. People and Tasks expose
  no general historical-revision API; no local history is represented as one.

Drive Labels and Drive Activity are distinct APIs and remain subsequent parent
Feature work. Docs/Sheets/Slides, setup flows and Admin SDK are separate builds.

## Method inventory

| Process | Vendor method | HTTP path | Request schema | Accepted scope suffixes |
|---|---|---|---|---|
| `drive_about_get` | `drive.about.get` | `GET drive/v3/about` | `none` | drive, drive.appdata, drive.file, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_accessproposals_get` | `drive.accessproposals.get` | `GET drive/v3/files/{fileId}/accessproposals/{proposalId}` | `none` | drive, drive.file, drive.metadata, drive.metadata.readonly, drive.readonly |
| `drive_accessproposals_list` | `drive.accessproposals.list` | `GET drive/v3/files/{fileId}/accessproposals` | `none` | drive, drive.file, drive.metadata, drive.metadata.readonly, drive.readonly |
| `drive_accessproposals_resolve` | `drive.accessproposals.resolve` | `POST drive/v3/files/{fileId}/accessproposals/{proposalId}:resolve` | `ResolveAccessProposalRequest` | drive, drive.file |
| `drive_approvals_approve` | `drive.approvals.approve` | `POST drive/v3/files/{fileId}/approvals/{approvalId}:approve` | `ApproveApprovalRequest` | drive, drive.file, drive.metadata |
| `drive_approvals_cancel` | `drive.approvals.cancel` | `POST drive/v3/files/{fileId}/approvals/{approvalId}:cancel` | `CancelApprovalRequest` | drive, drive.file, drive.metadata |
| `drive_approvals_comment` | `drive.approvals.comment` | `POST drive/v3/files/{fileId}/approvals/{approvalId}:comment` | `CommentApprovalRequest` | drive, drive.file, drive.metadata |
| `drive_approvals_decline` | `drive.approvals.decline` | `POST drive/v3/files/{fileId}/approvals/{approvalId}:decline` | `DeclineApprovalRequest` | drive, drive.file, drive.metadata |
| `drive_approvals_get` | `drive.approvals.get` | `GET drive/v3/files/{fileId}/approvals/{approvalId}` | `none` | drive, drive.appdata, drive.file, drive.metadata, drive.metadata.readonly, drive.readonly |
| `drive_approvals_list` | `drive.approvals.list` | `GET drive/v3/files/{fileId}/approvals` | `none` | drive, drive.appdata, drive.file, drive.metadata, drive.metadata.readonly, drive.readonly |
| `drive_approvals_reassign` | `drive.approvals.reassign` | `POST drive/v3/files/{fileId}/approvals/{approvalId}:reassign` | `ReassignApprovalRequest` | drive, drive.file, drive.metadata |
| `drive_approvals_start` | `drive.approvals.start` | `POST drive/v3/files/{fileId}/approvals:start` | `StartApprovalRequest` | drive, drive.file, drive.metadata |
| `drive_apps_get` | `drive.apps.get` | `GET drive/v3/apps/{appId}` | `none` | drive, drive.appdata, drive.apps.readonly, drive.file, drive.metadata, drive.metadata.readonly, drive.readonly |
| `drive_apps_list` | `drive.apps.list` | `GET drive/v3/apps` | `none` | drive.apps.readonly |
| `drive_changes_get_start_page_token` | `drive.changes.getStartPageToken` | `GET drive/v3/changes/startPageToken` | `none` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_changes_list` | `drive.changes.list` | `GET drive/v3/changes` | `none` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_changes_watch` | `drive.changes.watch` | `POST drive/v3/changes/watch` | `Channel` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_channels_stop` | `drive.channels.stop` | `POST drive/v3/channels/stop` | `Channel` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_comments_create` | `drive.comments.create` | `POST drive/v3/files/{fileId}/comments` | `Comment` | drive, drive.file |
| `drive_comments_delete` | `drive.comments.delete` | `DELETE drive/v3/files/{fileId}/comments/{commentId}` | `none` | drive, drive.file |
| `drive_comments_get` | `drive.comments.get` | `GET drive/v3/files/{fileId}/comments/{commentId}` | `none` | drive, drive.file, drive.meet.readonly, drive.readonly |
| `drive_comments_list` | `drive.comments.list` | `GET drive/v3/files/{fileId}/comments` | `none` | drive, drive.file, drive.meet.readonly, drive.readonly |
| `drive_comments_update` | `drive.comments.update` | `PATCH drive/v3/files/{fileId}/comments/{commentId}` | `Comment` | drive, drive.file |
| `drive_drives_create` | `drive.drives.create` | `POST drive/v3/drives` | `Drive` | drive |
| `drive_drives_delete` | `drive.drives.delete` | `DELETE drive/v3/drives/{driveId}` | `none` | drive |
| `drive_drives_get` | `drive.drives.get` | `GET drive/v3/drives/{driveId}` | `none` | drive, drive.readonly |
| `drive_drives_hide` | `drive.drives.hide` | `POST drive/v3/drives/{driveId}/hide` | `none` | drive |
| `drive_drives_list` | `drive.drives.list` | `GET drive/v3/drives` | `none` | drive, drive.readonly |
| `drive_drives_unhide` | `drive.drives.unhide` | `POST drive/v3/drives/{driveId}/unhide` | `none` | drive |
| `drive_drives_update` | `drive.drives.update` | `PATCH drive/v3/drives/{driveId}` | `Drive` | drive |
| `drive_files_copy` | `drive.files.copy` | `POST drive/v3/files/{fileId}/copy` | `File` | drive, drive.appdata, drive.file, drive.photos.readonly |
| `drive_files_create` | `drive.files.create` | `POST drive/v3/files` | `File` | drive, drive.appdata, drive.file |
| `drive_files_delete` | `drive.files.delete` | `DELETE drive/v3/files/{fileId}` | `none` | drive, drive.appdata, drive.file |
| `drive_files_download` | `drive.files.download` | `POST drive/v3/files/{fileId}/download` | `none` | drive, drive.file, drive.readonly |
| `drive_files_empty_trash` | `drive.files.emptyTrash` | `DELETE drive/v3/files/trash` | `none` | drive |
| `drive_files_export` | `drive.files.export` | `GET drive/v3/files/{fileId}/export` | `none` | drive, drive.file, drive.meet.readonly, drive.readonly |
| `drive_files_generate_cse_token` | `drive.files.generateCseToken` | `GET drive/v3/files/generateCseToken` | `none` | drive |
| `drive_files_generate_ids` | `drive.files.generateIds` | `GET drive/v3/files/generateIds` | `none` | drive, drive.appdata, drive.file |
| `drive_files_get` | `drive.files.get` | `GET drive/v3/files/{fileId}` | `none` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_files_list` | `drive.files.list` | `GET drive/v3/files` | `none` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_files_list_labels` | `drive.files.listLabels` | `GET drive/v3/files/{fileId}/listLabels` | `none` | drive, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.readonly |
| `drive_files_modify_labels` | `drive.files.modifyLabels` | `POST drive/v3/files/{fileId}/modifyLabels` | `ModifyLabelsRequest` | drive, drive.file, drive.metadata |
| `drive_files_update` | `drive.files.update` | `PATCH drive/v3/files/{fileId}` | `File` | drive, drive.appdata, drive.file, drive.metadata, drive.scripts |
| `drive_files_watch` | `drive.files.watch` | `POST drive/v3/files/{fileId}/watch` | `Channel` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_operations_get` | `drive.operations.get` | `GET drive/v3/operations/{name}` | `none` | drive, drive.file, drive.meet.readonly, drive.readonly |
| `drive_permissions_create` | `drive.permissions.create` | `POST drive/v3/files/{fileId}/permissions` | `Permission` | drive, drive.file |
| `drive_permissions_delete` | `drive.permissions.delete` | `DELETE drive/v3/files/{fileId}/permissions/{permissionId}` | `none` | drive, drive.file |
| `drive_permissions_get` | `drive.permissions.get` | `GET drive/v3/files/{fileId}/permissions/{permissionId}` | `none` | drive, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_permissions_list` | `drive.permissions.list` | `GET drive/v3/files/{fileId}/permissions` | `none` | drive, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_permissions_update` | `drive.permissions.update` | `PATCH drive/v3/files/{fileId}/permissions/{permissionId}` | `Permission` | drive, drive.file |
| `drive_replies_create` | `drive.replies.create` | `POST drive/v3/files/{fileId}/comments/{commentId}/replies` | `Reply` | drive, drive.file |
| `drive_replies_delete` | `drive.replies.delete` | `DELETE drive/v3/files/{fileId}/comments/{commentId}/replies/{replyId}` | `none` | drive, drive.file |
| `drive_replies_get` | `drive.replies.get` | `GET drive/v3/files/{fileId}/comments/{commentId}/replies/{replyId}` | `none` | drive, drive.file, drive.meet.readonly, drive.readonly |
| `drive_replies_list` | `drive.replies.list` | `GET drive/v3/files/{fileId}/comments/{commentId}/replies` | `none` | drive, drive.file, drive.meet.readonly, drive.readonly |
| `drive_replies_update` | `drive.replies.update` | `PATCH drive/v3/files/{fileId}/comments/{commentId}/replies/{replyId}` | `Reply` | drive, drive.file |
| `drive_revisions_delete` | `drive.revisions.delete` | `DELETE drive/v3/files/{fileId}/revisions/{revisionId}` | `none` | drive, drive.appdata, drive.file |
| `drive_revisions_get` | `drive.revisions.get` | `GET drive/v3/files/{fileId}/revisions/{revisionId}` | `none` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_revisions_list` | `drive.revisions.list` | `GET drive/v3/files/{fileId}/revisions` | `none` | drive, drive.appdata, drive.file, drive.meet.readonly, drive.metadata, drive.metadata.readonly, drive.photos.readonly, drive.readonly |
| `drive_revisions_update` | `drive.revisions.update` | `PATCH drive/v3/files/{fileId}/revisions/{revisionId}` | `Revision` | drive, drive.appdata, drive.file |
| `drive_teamdrives_create` | `drive.teamdrives.create` | `POST drive/v3/teamdrives` | `TeamDrive` | drive |
| `drive_teamdrives_delete` | `drive.teamdrives.delete` | `DELETE drive/v3/teamdrives/{teamDriveId}` | `none` | drive |
| `drive_teamdrives_get` | `drive.teamdrives.get` | `GET drive/v3/teamdrives/{teamDriveId}` | `none` | drive, drive.readonly |
| `drive_teamdrives_list` | `drive.teamdrives.list` | `GET drive/v3/teamdrives` | `none` | drive, drive.readonly |
| `drive_teamdrives_update` | `drive.teamdrives.update` | `PATCH drive/v3/teamdrives/{teamDriveId}` | `TeamDrive` | drive |
| `people_contact_groups_batch_get` | `people.contactGroups.batchGet` | `GET v1/contactGroups:batchGet` | `none` | contacts, contacts.readonly |
| `people_contact_groups_create` | `people.contactGroups.create` | `POST v1/contactGroups` | `CreateContactGroupRequest` | contacts |
| `people_contact_groups_delete` | `people.contactGroups.delete` | `DELETE v1/{+resourceName}` | `none` | contacts |
| `people_contact_groups_get` | `people.contactGroups.get` | `GET v1/{+resourceName}` | `none` | contacts, contacts.readonly |
| `people_contact_groups_list` | `people.contactGroups.list` | `GET v1/contactGroups` | `none` | contacts, contacts.readonly |
| `people_contact_groups_members_modify` | `people.contactGroups.members.modify` | `POST v1/{+resourceName}/members:modify` | `ModifyContactGroupMembersRequest` | contacts |
| `people_contact_groups_update` | `people.contactGroups.update` | `PUT v1/{+resourceName}` | `UpdateContactGroupRequest` | contacts |
| `people_other_contacts_copy_other_contact_to_my_contacts_group` | `people.otherContacts.copyOtherContactToMyContactsGroup` | `POST v1/{+resourceName}:copyOtherContactToMyContactsGroup` | `CopyOtherContactToMyContactsGroupRequest` | contacts, contacts.other.readonly |
| `people_other_contacts_list` | `people.otherContacts.list` | `GET v1/otherContacts` | `none` | contacts.other.readonly |
| `people_other_contacts_search` | `people.otherContacts.search` | `GET v1/otherContacts:search` | `none` | contacts.other.readonly |
| `people_people_batch_create_contacts` | `people.people.batchCreateContacts` | `POST v1/people:batchCreateContacts` | `BatchCreateContactsRequest` | contacts |
| `people_people_batch_delete_contacts` | `people.people.batchDeleteContacts` | `POST v1/people:batchDeleteContacts` | `BatchDeleteContactsRequest` | contacts |
| `people_people_batch_update_contacts` | `people.people.batchUpdateContacts` | `POST v1/people:batchUpdateContacts` | `BatchUpdateContactsRequest` | contacts |
| `people_people_connections_list` | `people.people.connections.list` | `GET v1/{+resourceName}/connections` | `none` | contacts, contacts.readonly |
| `people_people_create_contact` | `people.people.createContact` | `POST v1/people:createContact` | `Person` | contacts |
| `people_people_delete_contact` | `people.people.deleteContact` | `DELETE v1/{+resourceName}:deleteContact` | `none` | contacts |
| `people_people_delete_contact_photo` | `people.people.deleteContactPhoto` | `DELETE v1/{+resourceName}:deleteContactPhoto` | `none` | contacts |
| `people_people_get` | `people.people.get` | `GET v1/{+resourceName}` | `none` | contacts, contacts.other.readonly, contacts.readonly, directory.readonly, user.addresses.read, user.birthday.read, user.emails.read, user.gender.read, user.organization.read, user.phonenumbers.read, userinfo.email, userinfo.profile |
| `people_people_get_batch_get` | `people.people.getBatchGet` | `GET v1/people:batchGet` | `none` | contacts, contacts.other.readonly, contacts.readonly, directory.readonly, user.addresses.read, user.birthday.read, user.emails.read, user.gender.read, user.organization.read, user.phonenumbers.read, userinfo.email, userinfo.profile |
| `people_people_list_directory_people` | `people.people.listDirectoryPeople` | `GET v1/people:listDirectoryPeople` | `none` | directory.readonly |
| `people_people_search_contacts` | `people.people.searchContacts` | `GET v1/people:searchContacts` | `none` | contacts, contacts.readonly |
| `people_people_search_directory_people` | `people.people.searchDirectoryPeople` | `GET v1/people:searchDirectoryPeople` | `none` | directory.readonly |
| `people_people_update_contact` | `people.people.updateContact` | `PATCH v1/{+resourceName}:updateContact` | `Person` | contacts |
| `people_people_update_contact_photo` | `people.people.updateContactPhoto` | `PATCH v1/{+resourceName}:updateContactPhoto` | `UpdateContactPhotoRequest` | contacts |
| `tasks_tasklists_delete` | `tasks.tasklists.delete` | `DELETE tasks/v1/users/@me/lists/{tasklist}` | `none` | tasks |
| `tasks_tasklists_get` | `tasks.tasklists.get` | `GET tasks/v1/users/@me/lists/{tasklist}` | `none` | tasks, tasks.readonly |
| `tasks_tasklists_insert` | `tasks.tasklists.insert` | `POST tasks/v1/users/@me/lists` | `TaskList` | tasks |
| `tasks_tasklists_list` | `tasks.tasklists.list` | `GET tasks/v1/users/@me/lists` | `none` | tasks, tasks.readonly |
| `tasks_tasklists_patch` | `tasks.tasklists.patch` | `PATCH tasks/v1/users/@me/lists/{tasklist}` | `TaskList` | tasks |
| `tasks_tasklists_update` | `tasks.tasklists.update` | `PUT tasks/v1/users/@me/lists/{tasklist}` | `TaskList` | tasks |
| `tasks_tasks_clear` | `tasks.tasks.clear` | `POST tasks/v1/lists/{tasklist}/clear` | `none` | tasks |
| `tasks_tasks_delete` | `tasks.tasks.delete` | `DELETE tasks/v1/lists/{tasklist}/tasks/{task}` | `none` | tasks |
| `tasks_tasks_get` | `tasks.tasks.get` | `GET tasks/v1/lists/{tasklist}/tasks/{task}` | `none` | tasks, tasks.readonly |
| `tasks_tasks_insert` | `tasks.tasks.insert` | `POST tasks/v1/lists/{tasklist}/tasks` | `Task` | tasks |
| `tasks_tasks_list` | `tasks.tasks.list` | `GET tasks/v1/lists/{tasklist}/tasks` | `none` | tasks, tasks.readonly |
| `tasks_tasks_move` | `tasks.tasks.move` | `POST tasks/v1/lists/{tasklist}/tasks/{task}/move` | `none` | tasks |
| `tasks_tasks_patch` | `tasks.tasks.patch` | `PATCH tasks/v1/lists/{tasklist}/tasks/{task}` | `Task` | tasks |
| `tasks_tasks_update` | `tasks.tasks.update` | `PUT tasks/v1/lists/{tasklist}/tasks/{task}` | `Task` | tasks |
