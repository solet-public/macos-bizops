# g_suite_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:google-workspace

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's g_suite_plugin (Gmail, Calendar, Drive, Sheets, Docs, Slides) to one or more real Google accounts, Workspace or personal Gmail, surfaced during hydration if this plugin is present but not yet connected.

## Pitch

With `g_suite_plugin` installed but not connected to a Google account, every Google-touching verb returns `gsuite.not_connected` — the plugin is present and harmless, but does nothing. Connecting it lets the solet read and act on Gmail, Calendar, Drive, Sheets, Docs, and Slides directly, for itself and for peers acting through it.

This requires a real Google Workspace OAuth app and a one-time browser-based consent flow — genuine access to the operator's own Workspace account, not a sandboxed demo. Ask before doing any of it: "This solet can read and act on your Google Workspace (Gmail, Calendar, Drive, Sheets, Docs, Slides) if you connect an account. That needs a Google Cloud OAuth app and your explicit sign-in approval. Want to set this up now, or later?"

## Setup

Full step-by-step detail lives in this plugin's own overview article (`plugins/g_suite_plugin/knowledge_base/01_g_suite_overview.md`, "One-time operator setup") — this is the condensed version for the hydration conversation:

1. Google Cloud console → OAuth consent screen → user type = Internal for Workspace-only use, or External to also connect personal `@gmail.com` accounts (in Testing mode add each account as a test user; Google expires Testing-mode refresh tokens after 7 days, so set the app to In production for lasting access). Add the six scopes: `gmail.modify`, `gmail.settings.basic`, `drive`, `documents`, `spreadsheets`, `presentations` (the first two are Google restricted/sensitive scopes — a Workspace admin may need to allow them even for an Internal app).
2. Create an OAuth client, typed by deployment. Local solet: type **Desktop app** — no redirect URI registration needed; Google accepts the plugin's explicit IPv4 loopback callback (`http://127.0.0.1:<port>/oauth/google/callback`) on any port (proven flow, 2026-07-13). Cloud solet: type **Web application** with redirect URI `https://<solet-fqdn>/oauth/google/callback` (the ALB path-routes `/oauth/google/*` to the callback port; configure the callback bind host as `0.0.0.0` explicitly).
3. Store the client secret agent-blind in vault (`<solet>.default_address_book_plugin.google_client_secret`, via `store_from_file` — the secret must never enter model context).
4. Register the `google_oauth_app` address-book entry: `client_id` (literal), `client_secret` = `vault::<solet>.default_address_book_plugin.google_client_secret`, `redirect_uri`.
5. Connect: `start_interface` (a background job; wait for its result, the bound `callback_url`), then `connect_account` (optional `alias` such as work or home), operator opens the returned `authorize_url`, signs in with the account to connect and approves. The callback vaults that account's tokens. Repeat for each further account; `list_accounts` shows them all, and every verb takes an optional `account`.

On decline: stop, leave the plugin dormant. `gsuite.not_connected` on every verb is the fully-supported steady state, not a broken one.

For Drive search/metadata/permissions/comments/revisions/shared drives, Contacts
and Tasks, hydrate workspace_api_coverage.md and the exact process entry. It
lists all 102 methods, vendor-shaped parameters, scopes, paging and correction/
terminal operations. Every call accepts account and names it in completion.

Calendar uses the ten narrow scopes listed in the overview's "Google Calendar" section. Reconnect each existing account once through `connect_account` to grant them; a missing grant returns `gsuite.missing_scope` with reconnect steps.
