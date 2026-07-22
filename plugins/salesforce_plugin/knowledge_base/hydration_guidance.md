# salesforce_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:salesforce

Embedding Description: Operator-facing pitch and setup steps for connecting a homunculus's salesforce_plugin to a Salesforce org via the operator's sf CLI login (every verb shells out to the CLI directly — full delegation, no session ever held by the plugin), surfaced during hydration if this plugin is present but not yet configured.

## Pitch

With `salesforce_plugin` installed but no org registered, every Salesforce verb returns `sf.not_configured` — the plugin is present and harmless, but does nothing. Configuring it lets the homunculus query and act on a Salesforce org directly: SOQL queries, record reads, and — because this connector is full read/write (operator-ratified) — record creates, updates, and **permanent deletes**, all as the operator's own Salesforce user.

This requires only a Salesforce user account and one browser login — no new Connected App, no certificates, no admin change request (the flow rides the org's already-blessed Salesforce CLI app). Ask before doing any of it: "This homunculus can query and modify Salesforce as YOU — including creating, updating, and permanently deleting records under your username. That needs one browser login via the sf CLI. Want to set this up now, or later?"

## Setup

Full step-by-step detail lives in this plugin's own overview article (`plugins/salesforce_plugin/knowledge_base/01_salesforce_overview.md`, "One-time operator setup") — this is the condensed version for the hydration conversation:

1. Agent installs the **standalone** sf CLI bundle (ships its own Node — never npm onto the machine's ambient Node; that clash broke live) and pins its absolute path in the plugin config (`sf_cli_path`).
2. Operator logs in once: `sf org login web --alias <alias>` — the CLI keeps a keychain-backed refresh token; nothing re-prompts until logout or revocation.
3. Agent verifies the session token-safely (JSON-filter to instanceUrl/username — never echo raw `sf org display` output, it contains the access token).
4. Agent registers the `salesforce_org` address-book entry: `target_org` (the alias) + `instance_host` (the pinned my-domain host) — two literals, no secrets; the platform stores no Salesforce credential at all.
5. If the plugin is not in the live manifest, add it via the blue-green manifest deploy; then verify with `test_connection` — expect the operator's own username back.

On decline: stop, leave the plugin dormant. `sf.not_configured` on every verb is the fully-supported steady state, not a broken one — and the same dormancy returns automatically whenever the operator logs the CLI out of the org.
