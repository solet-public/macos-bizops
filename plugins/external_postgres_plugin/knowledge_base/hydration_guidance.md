# external_postgres_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:external-postgres

Embedding Description: Operator-facing guidance for external_postgres_plugin explaining that nothing is connected during hydration — connections to foreign Postgres databases (able to do whatever the registered credential's own database GRANTs allow: queries, writes, scripts, bulk loads, dumps and restores) are registered on demand, any number of them, via a repeatable per-connection wizard run whenever the operator wants to reach a database server.

## Pitch

This plugin is different from the one-time connectors: it is a **registry of named connections**, and an empty registry is its normal starting state — not dormancy. There is **nothing to set up during hydration.** Mention the capability in one line and move on: "This solet can work with any Postgres database to the extent the registered credential's own database GRANTs allow (a Datagrip replacement — introspection, queries, writes, migration scripts, CSV loads, dumps and restores, session and size monitoring). Nothing to configure now; whenever you want to reach a database server, we register that connection on the spot."

Connections are added on demand, any number of them, each scoped to one database server and each with its own credentials. What every verb can do on a connection is entirely the registered credential's own server-side Postgres GRANTs — this plugin performs no permission check, connection restriction or confirmation step of its own (USER ruling rul_4c99a30d; operator ruling 2026-08-09 + Amendment 1, "vendor RBAC is the control plane"). Consent is per-connection and happens at registration time, when the operator is naming a real host and picking which database role to register it under — not during hydration.

## Setup

This is a **repeatable per-connection wizard** — run it each time the operator asks to connect a database, once per connection. Full detail lives in this plugin's own overview article (`plugins/external_postgres_plugin/knowledge_base/01_external_postgres_overview.md`, "Registering a connection"):

1. **Pick a connection name** — it becomes the address-book entry `external_pg::<name>`. Registration is create-only: an existing name is refused (`NAME_EXISTS`), so pick a fresh name rather than expecting an overwrite.
2. **Collect the connection literals** — `host`, `port`, `dbname`, `user`, `sslmode` — from the operator directly or parsed out of an existing JDBC URL. Which database role to register matters now: what that role is GRANTed is exactly what every verb can do, writes and drops included — ask the operator which role fits the intended use (a role with only SELECT grants if this connection should never change data; a role with write, DDL or administrative grants where that work is wanted).
3. **Seed the password agent-blind** into vault at `<solet>.default_address_book_plugin.external_pg_<name>_password` — it never enters model context.
4. **Register the `external_pg::<name>` entry** (address_type `database`) with the literals plus `password` = the `vault::` reference from step 3.
5. **Verify** with `test_connection` naming the new connection — expect the resolved host, server version and current user echoed back (`read_only` is the server's own setting, false on an ordinary primary).

One structural guardrail: the platform's own database instance is refused by host+port+dbname tuple (`external_pg.platform_db_refused`) — other localhost databases are legitimate targets. Steady state with no (or few) connections is fully supported: `list_connections` simply returns what exists, and verbs naming an unregistered connection return `external_pg.connection_unknown`.
