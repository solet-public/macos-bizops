# Zuora Plugin (`zuora_plugin`)

A subscription-billing connector over the operator's Zuora tenant. Executor:
a plain synchronous `httpx.Client` (no official Zuora SDK, no CLI). Auth:
OAuth 2.0 client-credentials — the simplest auth model of the platform's
connector suite. Single tenant v1: the "zuora_tenant" address-book entry.

## Read/write posture — CREATE/UPDATE, NO DELETE

Unlike `snowflake_plugin`/`external_postgres_plugin` (read-only hard) or
`jira_plugin`/`salesforce_plugin` (full CRUD including delete), Zuora ships
**no delete verb at all**. Billing records (accounts, subscriptions,
invoices, payments) are voided or cancelled through Zuora's own workflow, not
deleted through this tool — deletion isn't a meaningful operation on most of
these object types in Zuora's own data model, so the connector simply
doesn't expose one.

## Session model — re-mintable bearer, in-memory only

The OAuth `client_secret` is the durable credential (chain-consumed through
the address book, never vaulted under this plugin's own identity). The
bearer token it mints is short-lived (~1h), re-mintable, and held ONLY in
process memory (`http_client.py::ZuoraClient`) — never persisted. A 401
triggers exactly ONE re-fetch-and-retry; a second 401 classifies as
`zuora.auth_failed`.

## Registering the OAuth client (operator console runbook, one-time)

1. **Create an OAuth client** in the Zuora tenant (Zuora admin console →
   Administration → Manage OAuth Clients) on a platform/API user with the
   permissions this connector needs (Data Query, Object API, Billing).
2. **Copy the client_id and client_secret** — Zuora surfaces the secret only
   once at creation.
3. **Register the address-book entry** `zuora_tenant` with the literal
   fields `base_url` (the environment selector — see below) and `client_id`,
   and a `client_secret` field holding
   `vault::<homunculus>.default_address_book_plugin.zuora_client_secret`.
   Ingest the secret agent-blind.

**`base_url` IS the environment selector** — there is no separate
prod/sandbox flag:

| Environment | `base_url` |
|---|---|
| US Production | `https://rest.zuora.com` |
| EU Production | `https://rest.eu.zuora.com` |
| Sandbox | `https://rest.apisandbox.zuora.com` |

## Security posture (mirrors the Jira/Snowflake/Salesforce/external-Postgres wave)

- **Foreign-target invariant.** No `base_url`/environment parameter on any
  verb — every request is built from the single registered `zuora_tenant`
  entry.
- **No export deny.** Not denied from the MCP surface — call `process_call` on
  these verbs directly. The prior RATIFY-3 deny was retired by operator ruling
  2026-07-15: friction, not security, on a single-user substrate where every
  MCP session is the operator (see
  `workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
- **Generic error messages for topology-leaking classes.** Auth and
  rate-limit errors NEVER embed the raw response body or request URL.
  Object-not-found, validation-failed, and query-failed errors describe the
  caller's own object/query via the response body's `reasons` list only.
- **Secrets hygiene.** The OAuth client_secret is chain-consumed through the
  address book's `resolve_with_secrets` — this plugin declares no vault keys
  of its own (`get_required_vault_keys`/`get_declared_vault_keys` both
  return `[]`). The re-minted bearer token is never vaulted.
- **Financial-data sensitivity.** Zuora records (invoices, payments,
  subscriptions) carry real billing PII — this connector's field
  sensitivities are set at the DB-connector floor (0.5), one notch above the
  generic-SaaS floor (0.3) used for Jira/Salesforce records, reflecting the
  higher consequence of a leaked invoice or payment row.

## SQL-lockdown gate note

This connector composes NO SQL-shaped strings anywhere — ZOQL queries flow
through as a caller-supplied JSON body field (`queryString`), never a
literal string the gate's S2 heuristic would match, and there is no database
driver import (`S0`). The SQL-lockdown gate is silent for this plugin — no
allowlist entry needed anywhere in `src/` or `tests/`.

## Design provenance

This plugin predates the 2026-07-09 umbrella hardening pass that ratified
Snowflake/Salesforce/Jira/external-Postgres (`workbench/2026-07-09_enterprise_connectors_design.md`,
which explicitly marks Zuora "out of scope, not in the operator's four").
The original design (`workbench/2026-06-20_zuora_plugin_design.md`) is a
single, lighter-weight revision. This build applies the umbrella's since-
established security posture (generic topology-safe error messages,
chain-consumed secrets, sync verb shape) on top of that original design
rather than re-litigating it from scratch. (The umbrella's process_export
deny posture was removed platform-wide 2026-07-15 — see the Security
posture section above.)

## Key files

| File | Purpose |
|---|---|
| `src/zuora_plugin/constants.py` | Every magic value: address-book field names, known base_url environments, error codes, caps. |
| `src/zuora_plugin/app_config.py` | Resolves `zuora_tenant` from the address book; the client_secret is chain-consumed. |
| `src/zuora_plugin/http_client.py` | `ZuoraClient` — synchronous httpx client with cached, re-mintable bearer auth (client-credentials grant). |
| `src/zuora_plugin/errors.py` | Topology-safe error classification (`classify_zuora_response`) from the response body's `reasons` list. |
| `src/zuora_plugin/billing_actions.py` | Pure verb implementations: `data_query`, `get_object`, `create_object`, `update_object`, `list_subscriptions`, `get_invoice`, `list_invoices`, `bulk_export`. |
| `src/zuora_plugin/plugin.py` | The `ZuoraPlugin` EDGE provider — client lifecycle, error mapping, EDGE registration. |
