# Google Workspace Vendor API Coverage

Article Layer: 1

Article Role: build-specification

Article Tags: planning-stage:solet-lifecycle, domain:google-workspace, domain:connector,
evidence-category:api-inventory

Embedding Description: Which Gmail API methods g_suite_plugin covers, method by
method: messages, threads, labels, drafts, attachments, history and profile
(build 1) and settings, filters, forwarding, delegates, send-as signatures,
S/MIME, client-side encryption and push watch (build 2) as typed verbs; Gmail
web features with no public API; the OAuth scope each verb needs; what a consumer Gmail
account gets versus a Workspace account; and the remaining Google Workspace
builds (Calendar now covers all 38 Calendar API v3 methods; see the overview).

## Source and rule

The method list below was rechecked against Google's live Gmail v1 discovery
revision `20261102` on 2026-11-07 (79 methods). Source:
`https://gmail.googleapis.com/$discovery/rest?version=v1`, SHA-256
`2fa858232e372a7f78fc36d180fd718a335050627e7c9480b8629a3cc82c1d5e`.
The offline regression fixture retains the 47 settings/push methods and their
request schemas from that response. Requests still use the installed client's
static discovery cache (`20260525`); the settings executor adds the current
`chainValidation` query directly to the authenticated request URI because that
parameter postdates the bundled document. Every method is a typed verb (32 in build 1, 47 in
build 2), and the Gmail web features absent from it are recorded below as
having no public API. None is silently
unsupported. Writes and deletes are first-class; the Google OAuth scope granted
to the account is the only boundary.

## Accounts and tiers

Several Google accounts can be connected at once (for example work and home).
Each is keyed by its Gmail address; every verb takes an optional `account`
(email or alias) and names the account it used. The tier is read from the Gmail
profile address at connect time:

| Tier | How detected | Gmail it gets |
|---|---|---|
| `consumer` | `@gmail.com` / `@googlemail.com` | Every build-1 verb. Build 2: watch/stop, vacation, filters, IMAP, POP, language, auto-forwarding, forwarding-address and send-as reads, send-as patch/update (signatures, `gmail.settings.basic`). Refused with `gsuite.workspace_only` naming the tier, before any request: delegates, forwarding-address create/delete, send-as create/delete/verify, auto-forwarding updates (`gmail.settings.sharing` is granted only to Workspace service accounts with domain-wide delegation), S/MIME and client-side encryption (Workspace features). |
| `workspace_user` | any other domain | Everything above, plus S/MIME, and CSE where the domain has client-side encryption (Enterprise Plus / Education Plus). `gmail.settings.sharing` still needs an admin-provisioned service account. |
| `workspace_admin` | reserved | Detection needs an Admin SDK scope and arrives with the admin build; admins read as `workspace_user` until then. |

A self-registered External OAuth client in Testing mode gets refresh tokens
that Google expires 7 days after consent; the plugin then returns
`gsuite.auth_expired` naming that cause and the reconnect steps.

## Scopes

The default consent requests `gmail.modify` and `gmail.settings.basic` (plus
Drive, Docs, Sheets and Slides). Settings reads use `gmail.modify`; settings
writes accept only `gmail.settings.basic` (an account connected before build 2
reconnects once for them, and gets `gsuite.missing_scope` naming it until then).
`gmail.settings.sharing` is not requested: Google grants it only to a service
account with domain-wide delegation, so those verbs explain that requirement
on a 403 instead of returning a bare one. `gmail.readonly`, `gmail.labels`, `gmail.compose`, `gmail.send` and
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

## Gmail build 2 — settings and push (47 methods)

Every row is declared once in `gmail_settings_spec.py` / `gmail_sharing_spec.py`
and checked against the discovery document by `smoke_gmail_settings.py`: HTTP
method, path, every path, query and writable body field, enums and scope.
Scope column: `modify` = gmail.modify (default), `basic` = gmail.settings.basic
(default since build 2), `sharing` = gmail.settings.sharing (service account
with domain-wide delegation only). WS = refused on a consumer account.

All 47 build-2 verbs accept the optional API-wide query parameters `fields`
(partial-response projection), `pretty_print` (`prettyPrint` on the wire), and
`quota_user` (`quotaUser`, at most 40 characters). Omission leaves Google's
defaults in effect. `gmail_create_cse_key_pair` also accepts `chain_validation`:
`all` checks the certificate chain and revocation (the default); `none` permits
imports for decrypting historical messages with invalid or revoked chains.
Those invalid chains cannot be assigned to a CSE identity. See the
[vendor method reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.cse.keypairs/create).

| Gmail method | Verb | Scope | Tier |
|---|---|---|---|
| users.watch | `gmail_watch` (topic_name required; label_ids, label_filter_behavior, label_filter_action) | modify | any |
| users.stop | `gmail_stop` | modify | any |
| users.settings.getAutoForwarding / updateAutoForwarding | `gmail_get_auto_forwarding` / `gmail_update_auto_forwarding` | modify / sharing | any / WS |
| users.settings.getImap / updateImap | `gmail_get_imap` / `gmail_update_imap` | modify / basic | any |
| users.settings.getPop / updatePop | `gmail_get_pop` / `gmail_update_pop` | modify / basic | any |
| users.settings.getLanguage / updateLanguage | `gmail_get_language` / `gmail_update_language` | modify / basic | any |
| users.settings.getVacation / updateVacation | `gmail_get_vacation` / `gmail_update_vacation` | modify / basic | any |
| users.settings.filters.list / get | `gmail_list_filters` / `gmail_get_filter` | modify | any |
| users.settings.filters.create / delete | `gmail_create_filter` / `gmail_delete_filter` | basic | any |
| users.settings.forwardingAddresses.list / get | `gmail_list_forwarding_addresses` / `gmail_get_forwarding_address` | modify | any |
| users.settings.forwardingAddresses.create / delete | `gmail_create_forwarding_address` / `gmail_delete_forwarding_address` | sharing | WS |
| users.settings.delegates.list / get | `gmail_list_delegates` / `gmail_get_delegate` | modify | WS |
| users.settings.delegates.create / delete | `gmail_create_delegate` / `gmail_delete_delegate` | sharing | WS |
| users.settings.sendAs.list / get | `gmail_list_send_as` / `gmail_get_send_as` | modify | any |
| users.settings.sendAs.patch / update | `gmail_patch_send_as` / `gmail_update_send_as` (signature, display name, reply-to, default, alias, SMTP relay) | basic | any |
| users.settings.sendAs.create / delete / verify | `gmail_create_send_as` / `gmail_delete_send_as` / `gmail_verify_send_as` | sharing | WS |
| users.settings.sendAs.smimeInfo.list / get | `gmail_list_smime_info` / `gmail_get_smime_info` | modify | WS |
| users.settings.sendAs.smimeInfo.insert / setDefault / delete | `gmail_insert_smime_info` / `gmail_set_default_smime_info` / `gmail_delete_smime_info` | basic | WS |
| users.settings.cse.identities.list / get | `gmail_list_cse_identities` (page_size, page_token) / `gmail_get_cse_identity` | modify | WS |
| users.settings.cse.identities.create / patch / delete | `gmail_create_cse_identity` / `gmail_patch_cse_identity` / `gmail_delete_cse_identity` | basic | WS |
| users.settings.cse.keypairs.list / get | `gmail_list_cse_key_pairs` (page_size, page_token) / `gmail_get_cse_key_pair` | modify | WS |
| users.settings.cse.keypairs.create / enable / disable / obliterate | `gmail_create_cse_key_pair` / `gmail_enable_cse_key_pair` / `gmail_disable_cse_key_pair` / `gmail_obliterate_cse_key_pair` | basic | WS |

Push is explicit only: nothing subscribes unless `gmail_watch` is called with
a Pub/Sub topic the caller owns (it must grant publish to
`gmail-api-push@system.gserviceaccount.com`); a watch expires after about
7 days and is renewed by calling it again. An unknown tier (an account
connected before tiers were recorded) is never refused. Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.

## No public API

Absent from all 79 methods: scheduled send, snooze, confidential mode, the
undo-send delay, inbox type and category-tab configuration (the `CATEGORY_*`
labels can be read and changed on messages, but tabs cannot be configured),
multiple-inbox and reading-pane layout, templates, smart features, read
receipts, and Chat/Spaces (a separate Chat API).

## Remaining Google Workspace builds

Calendar build 2 now covers all 38 Calendar API v3 methods (see the overview),
including API-wide `fields`, `pretty_print`, `alt`, `quota_user` and deprecated
`user_ip` on every verb. Credentials remain owned by the account contract;
`key`, `access_token` and `oauth_token` are declared only to route their named
account-contract refusal through framework submission. Calendar responses
are returned as received.
Calendar is presented as part of the Google connector. Later builds cover the Drive
remainder (search queries, metadata, permissions, comments, export formats),
Contacts (People API), Tasks, the Docs and Sheets remainder, and the Admin
console. Each build ships typed verbs, a knowledge-base process entry per verb
and a live smoke on a connected account.

### Gmail settings API-wide response controls

Every one of the 47 settings/push verbs accepts `fields`, `pretty_print`,
`quota_user`, `xgafv` (Google `$.xgafv`: `1` or `2`) and `alt=json`.
Omitting `xgafv` uses the vendor default; changing it on a later call changes
that request only. Non-JSON `alt=proto` or `alt=media` is rejected as
`gsuite.invalid_params` before a vendor request, with a message naming the
JSON result contract. It is never silently replaced with JSON. The connected
account supplies authentication; these controls do not change its grant.

### Deferred jobs

Gmail settings dispatch and queued jobs use the same deferred path as every
other Google verb. Existing completion metadata identifies a Gmail settings job
before its request is read, and the queued request must name the same verb.
Results and errors are returned as received; the plugin does not retry an
uncertain write.
