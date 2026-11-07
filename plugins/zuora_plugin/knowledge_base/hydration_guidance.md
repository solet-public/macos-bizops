# zuora_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:zuora

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's zuora_plugin to a Zuora billing tenant with OAuth client-credentials (workflow backup and restore, workflow runs and tasks, SQL Data Query jobs and file downloads, orders, subscription lifecycle, customer accounts, payment methods, payments and refunds, custom objects, Object Query reads, invoices, credit and debit memos, bill runs, usage, object create/update, and authenticated vendor-authorized reads, writes, and deletes), surfaced during hydration if this plugin is present but not yet connected.

## Pitch

With `zuora_plugin` installed but no tenant registered, every Zuora verb returns `zuora.not_configured` — the plugin is present and harmless, but does nothing. Connecting it lets the solet work a Zuora billing tenant directly. Vendor RBAC on the registered role is the only capability boundary: the plugin adds no read-only gate, and reads, writes and deletes are equally first-class. Typed operations cover Workflows (list, read, update, delete, export to a JSON backup, import, versions, runs, tasks, usage, callout auth providers), SQL Data Query jobs (submit, list, read, cancel, download the result), Files (download, status, restore), Orders (preview, create, list, read, correct, activate, cancel, delete, async jobs), Subscriptions (preview, create, read and version history, amend, renew, suspend, resume, cancel, delete, metrics), Accounts (create, read, summary, correct, delete, payment methods and cascading), Payment Methods (create, wallet-token create, read, correct, verify, balance, scrub, delete, stored credential profiles, wallet domains, Payment Form sessions), Payments (create, list, read, parts, correct, apply, unapply, transfer, refund, cancel, delete), Custom Objects (definitions, records with batch update and delete, CSV bulk jobs), Billing documents and invoices (list, generate, create standalone, correct, post, unpost, split, email, PDF files and status, items and taxes, reverse, write off, cancel, delete), Credit memos (create, correct, post, unpost, apply, unapply, refund, reverse, write off, cancel, delete, files, items, parts), Debit memos (create, correct, post, unpost, collect, write off, cancel, delete, files, items), Bill runs (create, read, post, cancel, delete, email documents), Usage (file import, record create, read, correct, delete, rate detail), Object Query (list or read any standard or custom object with filters and expansion), ZOQL `data_query` and `bulk_export`, object create/update and billing reads, plus `test_connection`. The generic `request` verb reaches any other relative tenant path with any method. Billing records Zuora voids or cancels keep that vendor-defined state.

Auth is OAuth client-credentials — the simplest in the connector suite: one client_id + client_secret pair, no browser flow, no callback server, no token expiry ceremony (short-lived bearers are re-minted on demand and held only in memory). Ask before doing any of it: "This solet can read and modify your Zuora billing tenant through typed operations and the generic authenticated `request` verb — including workflow backups and restores, workflow runs, SQL Data Query jobs, file downloads, orders, subscriptions, customer accounts, and vendor reads, writes, deletes and lifecycle actions wherever your registered role permits them. Grant the vendor role the work needs; the role decides what is allowed, and the plugin does not impose a read-only gate. That needs one OAuth client created in the Zuora admin UI (~2 minutes). Want to set this up now, or later?"

## Setup

Full step-by-step detail lives in this plugin's own overview article (`plugins/zuora_plugin/knowledge_base/01_zuora_overview.md`, "Registering the tenant") — this is the condensed version for the hydration conversation:

1. **OAuth client.** Operator creates one in the Zuora admin UI against an API-appropriate user, and copies the `client_id` and `client_secret` immediately — Zuora shows the secret exactly once at creation.
2. **Pick the `base_url` — it IS the environment selector** (no separate prod/sandbox flag): `https://rest.zuora.com` (US production), `https://rest.eu.zuora.com` (EU production), or `https://rest.apisandbox.zuora.com` (sandbox). Point at the sandbox first if one exists.
3. **Seed the client secret agent-blind** into vault at `<solet>.default_address_book_plugin.zuora_client_secret` — it never enters model context.
4. **Register the `zuora_tenant` address-book entry**: literals `base_url` and `client_id`, plus `client_secret` = the `vault::` reference from step 3.
5. If the plugin is not in the live manifest, add it via the blue-green manifest deploy. Then verify with `test_connection` — expect tenant identity back, confirming the entry points at the environment you intended.

On decline: stop, leave the plugin dormant. `zuora.not_configured` on every verb is the fully-supported steady state, not a broken one.
