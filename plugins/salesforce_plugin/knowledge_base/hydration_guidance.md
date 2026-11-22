# salesforce_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:salesforce

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's salesforce_plugin to one or more Salesforce orgs via the operator's sf CLI login (every verb shells out to the CLI directly — full delegation, no session ever held by the plugin), surfaced during hydration if this plugin is present but no org is bound yet.

## Pitch

With `salesforce_plugin` installed but no org bound, every Salesforce verb returns `sf.not_configured` — the plugin is present and harmless, but does nothing. Binding orgs lets the solet query and act on them directly, several at once (production, sandboxes, scratch orgs and a Dev Hub, each named by an alias), and manage their lifecycle (scratch orgs from a Dev Hub, sandboxes created, cloned, refreshed and deleted): SOQL queries, record reads, and — because this connector is full read/write (operator-ratified) — record creates, updates, and **permanent deletes** (one record, composite and Collections batches, records by external id, Bulk API 2.0 jobs including hardDelete), report and dashboard saves and deletes, invocable actions and flows, Tooling API changes and anonymous Apex, metadata deployments (including destructive changes), Salesforce Files uploads and deletes, and scratch org and sandbox deletes, all as the operator's own Salesforce user in each org. Salesforce's own permissions on that user are the only boundary; there is no read-only integration user to set up.

This requires only a Salesforce user account and one browser login — no new Connected App, no certificates, no admin change request (the flow rides the org's already-blessed Salesforce CLI app). Ask before doing any of it: "This solet can query and modify Salesforce as YOU — including creating, updating, and permanently deleting records under your username. That needs one browser login via the sf CLI. Want to set this up now, or later?"

## Setup

Full step-by-step detail lives in this plugin's own overview article (`plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md`, "One-time operator setup") — this is the condensed version for the hydration conversation:

1. Agent calls `plugin::salesforce_plugin::provision_cli` only after explicit system-change acknowledgement. It probes first, installs the Homebrew `sf` formula and its declared Node closure only when absent, then binds the verified absolute path in `sf_cli_path`; the returned boundary states that no reload or restart is hidden.
2. For each org: agent calls `login` with method `web` (and `alias`); the operator signs in once in the browser that opens. The CLI keeps a keychain-backed refresh token; nothing re-prompts until logout or revocation. An org the CLI is already logged in to is bound with `bind_org` instead. The first org bound is the default; `set_default_org` changes it.
3. For a solet that must log in with no one at the browser, the operator stores a connected app's private key in the vault with `vault_service store_from_file` under `<solet>.salesforce_plugin.org_secret__<name>`, and the agent calls `login` method `jwt` with `jwt_key_secret: <name>`. The agent never sees or passes the key itself.
4. If the plugin is not in the live manifest, add it via the blue-green manifest deploy; then verify with `test_connection` (and `list_orgs`) — expect the operator's own username back. A solet set up before multi-org support keeps working: its `salesforce_org` address-book entry is bound automatically on first use.
5. For Bulk ingest CSVs, metadata zip packages, Files uploads, scratch org and sandbox definition files, and downloaded results, save the input/output folder as a path address: say "save exports for <project> in <folder>". The same saved locations serve reads and writes, alongside the session folder and solet home. Paths must stay outside the installation tree; input files must already exist.

On decline: stop, leave the plugin dormant. `sf.not_configured` on every verb is the fully-supported steady state, not a broken one — and the same dormancy returns for an org whenever the operator logs the CLI out of it.
