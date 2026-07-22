# External Postgres Plugin (`external_postgres_plugin`)

A read-only **"super Datagrip"** over **foreign** Postgres databases the
operator registers — cloud RDS or local dev Postgres. It is deliberately NOT
`postgres_state_management_plugin` (the platform's own state-DB owner): the words
"external" vs "state_management" keep the roles disjoint in registry listings, KB
retrieval, and gate reports. This plugin never touches the platform's own
database; the containment guard refuses it role-independently.

Every verb takes a connection **NAME** (never a DSN). Adding a database is a
one-time operator **registration** — no code change.

## Read/write posture — READ-ONLY, HARD (operator-ratified, RATIFY-2)

The operator's dividing line: *"if someone deleted a database that would be a big
deal."* Because this is a developer tool used by multiple people, a stray
`DELETE FROM orders` / `DROP TABLE` must be structurally impossible. So there is
**no write verb**, and the read-only guarantee is enforced at three layers:

1. **LOAD-BEARING — the psycopg3 connection read-only characteristic.**
   `conn.read_only = True` is set BEFORE the first `execute()`, so psycopg3 emits
   the read-only characteristic at the `BEGIN` of *every* transaction including
   the first implicit one — there is **no write-capable window**. Every write
   (INSERT/UPDATE/DELETE/MERGE/CREATE/**CREATE TEMP**/DDL) fails at the server
   with **SQLSTATE 25006** on the first statement, regardless of statement
   leader, count, or smuggling — even `EXPLAIN ANALYZE <write>`, and even for an
   over-privileged registered credential. This is NOT a post-connect
   `SET default_transaction_read_only` (that would leave the first implicit
   transaction write-capable — the Codex BLOCKER this design fixed).
2. **BELT — the read-leader guard.** Admits only the Datagrip read family
   `{SELECT, WITH, EXPLAIN, SHOW, VALUES, TABLE}`; everything else fails fast with
   `external_pg.read_only_violation`. Defense-in-depth + UX, NOT the boundary.
3. **BELT — single-statement per call** via a real SQL parser (`sqlparse`, BSD).
   The parser is **result-shape hygiene** (bounds `SET`-injection + multi-result),
   NOT write containment: a second statement that slips `sqlparse` still cannot
   write, because the read-only connection refuses it (25006). `sqlparse` was
   ratified over `pglast` (GPL-3.0) because the repo is publicly distributed and
   the parser is not the destruction boundary.

**v1 limitation:** `CREATE TEMP TABLE` / scratch writes are refused (they are
writes). CTEs are the covered path for scratch computation. A temp-schema-write
mode is a deliberate operator-gated **v2** decision, not a config flip.

Registration should still recommend a **read-only DB user** (defense-in-depth);
`test_connection` surfaces the connecting role so a misconfigured write-capable
credential is visible.

## Containment — never the platform's own database (§8.4)

The plugin can reach ONLY DSNs that exist as `external_pg::*` address-book
entries (no DSN/URL parameter on any verb). On top of that, `assert_foreign_target`
refuses the platform's own DB **instance** — keyed on `(host, port, dbname)`,
**role-independently**:

- refused: `dbname == HOMUNCULUS_NAME` **AND** host ∈ {`localhost`, `127.0.0.1`, `::1`,
  the unix socket} **AND** `port == platform_pg_port`. This catches every role
  bound to the platform database — the refusal keys on the instance, not the role.
- allowed: a dev DB on `localhost` with a different dbname, or a same-named DB
  on a different host/port. localhost is a legitimate target *class*.

`_normalize_host` collapses a blank host **and** any absolute-path unix-socket
spelling (`/tmp`, `/var/run/postgresql`) to the socket sentinel, and a blank
port defaults to 5432 — so neither a blank-host nor a blank-port registration can
slip the guard. **Accepted v1 residual:** a non-loopback *alias* that resolves to
the local platform host would slip the literal host check; it requires a contrived
mis-registration, the read-only connection bounds any reach to reads, and the
registered-entries-only invariant is the primary containment. DNS/host-resolution
hardening + the cloud RDS-instance tuple are a v2 item.

### The `platform_pg_port` footgun

The guard compares against the platform's own Postgres port, read from THIS
plugin's own config (`plugin.yaml` `platform_pg_port`, default **5432**) — never
from the state plugin's config (that coupling is what the design avoids). **If the
platform ever runs Postgres off 5432, update `platform_pg_port` in this plugin's
config** or the guard will not recognize the platform instance.

## Exposure — direct `process_call`, same as any other plugin

These verbs are NOT export-denied. The prior RATIFY-3 deny that gated this
plugin (and jira/snowflake/salesforce/zuora) was retired by operator ruling
2026-07-15: on this single-user substrate the deny was friction, not security
— every MCP session is the operator — and bulk row data belongs in workspace
TSV exports rather than context (see
`workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
Call `process_call` on these verbs directly, same as any other registered
process.

## Topology-safe errors

Driver exception strings embed host/port/db/user topology. So connection / auth /
permission / timeout error classes carry a **generic fixed message** and NEVER
`str(exc)`. Only the caller's-own-query classes (syntax, undefined object) carry
the server's primary diagnostic message (which describes the caller's SQL, not our
topology). Row payloads and result-blob keys carry `data_sensitivity` 0.5.

## Registering a connection (operator runbook)

Each connection is one address-book entry `external_pg::<name>` with literal
`host`/`port`/`dbname`/`user`/`sslmode` fields plus a `vault::` password
reference. The password goes into vault **agent-blind** under the address-book
resolver's own namespace
(`<homunculus>.default_address_book_plugin.external_pg_<name>_password`) and is
chain-consumed via `resolve_with_secrets` — this plugin owns no vault keys and
needs no vault binding.

```
process_call service_interface::address_book_service::register {
  "name": "external_pg::analytics",
  "address_type": "database",
  "description": "Read-only analytics RDS",
  "entries": [
    {"field_type": "host",     "value": "analytics.abc123.us-west-2.rds.amazonaws.com"},
    {"field_type": "port",     "value": "5432"},
    {"field_type": "dbname",   "value": "analytics"},
    {"field_type": "user",     "value": "analytics_readonly"},
    {"field_type": "sslmode",  "value": "require"},
    {"field_type": "password", "value": "vault::<homunculus>.default_address_book_plugin.external_pg_analytics_password"}
  ]
}
```

- **Registration is create-only.** `register` fails on a duplicate name
  (`NAME_EXISTS`) — re-registering does NOT silently repoint an existing
  connection; changing a target is a deliberate delete-then-register (operator
  action).
- **JDBC-URL convenience.** The `app_config.parse_jdbc_url` helper decomposes a
  `jdbc:postgresql://user:pass@host:port/dbname?sslmode=require` (or plain
  `postgresql://…`) URL into the discrete fields for registration. The password is
  extracted to its own field and **never logged** — both the resolved DSN and the
  parsed-registration objects redact the password in `repr`, and parse failures
  scrub the URL.
- `test_connection` returns the resolved `server_version`, `current_user`, and
  `read_only` so the operator can confirm a connection points where they expect
  before trusting it.

## Verb map (all EDGE, all reads)

| Verb | Args | Returns |
|---|---|---|
| `run_query` | `connection_name, sql, max_rows=200` | rows inline (columns/rows/row_count/spilled=false); fails loud with `external_pg.result_too_large` over the inline caps |
| `list_connections` | — | `{connections: [name]}` (names only, never secrets) |
| `list_schemas` | `connection_name` | `{schemas: [name]}` |
| `list_tables` | `connection_name, schema` | `{tables: [{name, kind}]}` |
| `describe_table` | `connection_name, schema, table` | `{columns: [{name, type, nullable, default}]}` |
| `export_query` | `connection_name, sql, output_tsv_path` | `{path, columns, row_count, truncated}` — full result (up to 50000 rows) written as ONE `.tsv` file at the caller's ABSOLUTE path |
| `test_connection` | `connection_name` | `{ok, server_version, current_user, read_only}` |

`export_query` writes to the operator's OWN workspace, never platform blob
storage (operator ruling 2026-07-15: bulk business data belongs in workspace
files). The path must be absolute, end in `.tsv`, and lie under an
operator-configured `export_allowed_roots` entry in this plugin's config —
realpath + `commonpath` containment mirroring `ledger_allowed_roots`. The
default `[]` REFUSES every export until the operator opts roots in; refusals
name the config key. There is no session-cwd inference and no `Path.cwd()` —
the caller supplies the path, the config supplies the containment.

`run_query` returns up to `max_rows` (default 200, hard cap 1000); a result larger
than the inline row/byte cap FAILS LOUD (A4, 2026-07-16 — no blob spill). For the
full result set as a file, use `export_query`. Rows are returned as **arrays
parallel to `columns`** (not
name-keyed dicts) so a JOIN's duplicate column names (`SELECT a.id, b.id`) never
collapse and lose a value. Non-primitive column values (timestamps, Decimals,
UUIDs) are coerced to their string form in the result.

## Dependencies + delivery

`psycopg[binary]>=3.2` (the executor; binary wheel avoids a libpq build dep) and
`sqlparse>=0.5` (the single-statement parser). Delivered **INERT**: after landing,
the operator runs `pip install -e plugins/external_postgres_plugin` into the shared
`.venv`. Until installed the plugin is dormant tree-ware.

The hermetic smokes run without a database. The two LIVE smokes
(`smoke_readonly.py`, `smoke_multistatement.py`) prove the read-only boundary
against a local **scratch** Postgres database (`epg_smoke_scratch` on the Homebrew
cluster — NEVER the platform database) and skip cleanly when that fixture is unreachable.
