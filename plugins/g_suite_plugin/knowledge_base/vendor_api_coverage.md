# Google Workspace Vendor API Coverage

Article Layer: 1

Article Role: build-specification

Article Tags: planning-stage:solet-lifecycle, domain:google-workspace, domain:connector,
evidence-category:api-inventory

Embedding Description: Which Gmail API methods g_suite_plugin covers, method by
method: messages, threads, labels, drafts, attachments, history and profile as
typed verbs; Gmail settings and push watch as the next build; Gmail web features
with no public API; the OAuth scope each verb needs; what a consumer Gmail
account gets versus a Workspace account; and the remaining Google Workspace
builds (Calendar next).

## Source and rule

The method list below is the Gmail v1 discovery document that ships inside the
google-api-python-client package (its static discovery cache; discovery revision
20260525: 79 methods). It is the document the plugin's
client is built from, so it is exactly what the plugin can call; recheck it when
the library is upgraded. Every method is either a typed verb, assigned to a
named later build, or recorded as having no public API. None is silently
unsupported. Writes and deletes are first-class; the Google OAuth scope granted
to the account is the only boundary.

## Accounts and tiers

Several Google accounts can be connected at once (for example work and home).
Each is keyed by its Gmail address; every verb takes an optional `account`
(email or alias) and names the account it used. The tier is read from the Gmail
profile address at connect time:

| Tier | How detected | Gmail it gets |
|---|---|---|
| `consumer` | `@gmail.com` / `@googlemail.com` | Every build-1 verb. Build 2: vacation, filters, IMAP, POP, language, forwarding-address and send-as reads, signatures (`gmail.settings.basic`). Not delegates, forwarding-address or send-as create/delete, auto-forwarding updates (`gmail.settings.sharing` is granted only to Workspace service accounts with domain-wide delegation), S/MIME or client-side encryption (Workspace features). |
| `workspace_user` | any other domain | Everything above, plus S/MIME, and CSE where the domain has client-side encryption (Enterprise Plus / Education Plus). `gmail.settings.sharing` still needs an admin-provisioned service account. |
| `workspace_admin` | reserved | Detection needs an Admin SDK scope and arrives with the admin build; admins read as `workspace_user` until then. |

A self-registered External OAuth client in Testing mode gets refresh tokens
that Google expires 7 days after consent; the plugin then returns
`gsuite.auth_expired` naming that cause and the reconnect steps.

## Scopes

The default consent requests `gmail.modify` (plus Drive, Docs, Sheets and
Slides). `gmail.readonly`, `gmail.labels`, `gmail.compose`, `gmail.send` and
`gmail.insert` are strict subsets of it, so requesting them as well would only
lengthen the consent screen. Permanent delete accepts only
`https://mail.google.com/`, which is requested only when `connect_account` is
called with `gmail_permanent_delete=true`; without it the three delete verbs
return `gsuite.missing_scope` naming that step. Refresh never re-sends scopes,
so adding a scope never breaks an account connected earlier; that account keeps
its grant until it reconnects (`test_connection` lists `missing_default_scopes`).

## Gmail build 1 — typed verbs (32 methods)

| Gmail method | Verb | Scope used |
|---|---|---|
| users.getProfile | `gmail_get_profile` | gmail.modify |
| users.messages.list | `gmail_list_messages` (query, label_ids, page_token) | gmail.modify |
| users.messages.get | `gmail_get_message` (format full, raw, metadata, minimal) | gmail.modify |
| users.messages.send | `gmail_send` (cc, bcc, HTML, attachments, replies) | gmail.modify |
| users.messages.insert | `gmail_insert_message` | gmail.modify |
| users.messages.import | `gmail_import_message` | gmail.modify |
| users.messages.modify | `gmail_modify_message` (mark_read, archive, star, labels) | gmail.modify |
| users.messages.batchModify | `gmail_batch_modify_messages` | gmail.modify |
| users.messages.trash / untrash | `gmail_trash_message` / `gmail_untrash_message` | gmail.modify |
| users.messages.delete | `gmail_delete_message` | mail.google.com (opt-in) |
| users.messages.batchDelete | `gmail_batch_delete_messages` | mail.google.com (opt-in) |
| users.messages.attachments.get | `gmail_get_attachment` (blob or local path) | gmail.modify |
| users.threads.list / get | `gmail_list_threads` / `gmail_get_thread` | gmail.modify |
| users.threads.modify | `gmail_modify_thread` | gmail.modify |
| users.threads.trash / untrash | `gmail_trash_thread` / `gmail_untrash_thread` | gmail.modify |
| users.threads.delete | `gmail_delete_thread` | mail.google.com (opt-in) |
| users.labels.list / get | `gmail_list_labels` / `gmail_get_label` | gmail.modify |
| users.labels.create / patch / update / delete | `gmail_create_label` / `gmail_patch_label` / `gmail_update_label` / `gmail_delete_label` | gmail.modify |
| users.drafts.create / get / list / update / send / delete | `gmail_create_draft` / `gmail_get_draft` / `gmail_list_drafts` / `gmail_update_draft` / `gmail_send_draft` / `gmail_delete_draft` | gmail.modify |
| users.history.list | `gmail_list_history` | gmail.modify |

## Gmail build 2 — settings and push (47 methods, next unit)

- users.watch, users.stop: push notifications; the caller supplies a Cloud
  Pub/Sub topic that grants publish to `gmail-api-push@system.gserviceaccount.com`.
- users.settings get/update AutoForwarding, Imap, Language, Pop, Vacation.
- settings.filters, settings.forwardingAddresses, settings.delegates:
  create, delete, get, list.
- settings.sendAs create, delete, get, list, patch, update, verify;
  settings.sendAs.smimeInfo delete, get, insert, list, setDefault.
- settings.cse.identities create, delete, get, list, patch;
  settings.cse.keypairs create, disable, enable, get, list, obliterate.

Settings writes need `gmail.settings.basic`; the `gmail.settings.sharing`
methods need a Workspace service account with domain-wide delegation. A verb
that a tier cannot use must refuse with one message naming the tier, never a
bare 403.

## No public API

Absent from all 79 methods: scheduled send, snooze, confidential mode, the
undo-send delay, inbox type and category-tab configuration (the `CATEGORY_*`
labels can be read and changed on messages, but tabs cannot be configured),
multiple-inbox and reading-pane layout, templates, smart features, read
receipts, and Chat/Spaces (a separate Chat API).

## Remaining Google Workspace builds

Build 2 is Calendar (calendars, events with time ranges and paging, free/busy),
presented as part of the Google connector. Later builds cover the Drive
remainder (search queries, metadata, permissions, comments, export formats),
Contacts (People API), Tasks, the Docs and Sheets remainder, and the Admin
console. Each build ships typed verbs, a knowledge-base process entry per verb
and a live smoke on a connected account.
