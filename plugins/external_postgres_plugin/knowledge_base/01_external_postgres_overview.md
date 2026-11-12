# External Postgres Plugin (`external_postgres_plugin`)

A **"super Datagrip"** over **foreign** Postgres databases the operator
registers — cloud RDS or local dev Postgres. It is deliberately NOT
`postgres_state_management_plugin` (the platform's own state-DB owner): the words
"external" vs "state_management" keep the roles disjoint in registry listings, KB
retrieval, and gate reports. This plugin never touches the platform's own
database; the containment guard refuses it role-independently, for every verb.
The family-by-family map of PostgreSQL capabilities to verbs, with the next
builds, is `vendor_api_coverage.md` in this knowledge base.

Every verb takes a connection **NAME** (never a DSN). Adding a database is a
one-time operator **registration** — no code change.

## Access posture — the role's GRANTs are the only boundary

The plugin reaches the full PostgreSQL capability the registered role is
granted on the server, writes and deletes included. Per the USER ruling
`rul_4c99a30d` (2026-11-06), which completed the operator's 2026-08-09 reversal
and its Amendment 1 (*"these systems all have RBAC and we do not [need] to try
to re-implement controls in our plugins"*):

- No verb opens a restricted connection. Every verb uses an ordinary
  transactional connection, and the server's own GRANTs, role attributes and
  row-security policies decide what each statement may do.
- No verb refuses a statement by its leading keyword. `run_query` and
  `export_query` run any one statement and commit after its rows are written
  to the TSV, so a data-modifying CTE (`WITH d AS (DELETE … RETURNING …)
  SELECT …`) takes effect.
- No verb asks for confirmation before a destructive operation.
  `run_statement`, `run_script`, `copy`, `restore_database` (with `clean`) and
  `activity` `terminate` do exactly what the role is allowed to do.
- The single-statement rule on `run_query`, `export_query` and
  `run_statement` is a result-shape contract, not a permission check:
  `run_script` runs several statements in one transaction.

A server that is itself read-only (a hot standby, or a role whose
`default_transaction_read_only` is on) still refuses writes with SQLSTATE
25006; the plugin reports that as `external_pg.read_only_sql_transaction`, the
server's own decision.

One thing is containment of THIS host, not access control, and stays:
`restore_database` takes pg_dump archives only (through `pg_restore`), never plain SQL through `psql`, whose
backslash meta-commands (`\!`, `\o |cmd`, `\i`, `\copy … program` …) would run on the
Solet's own machine where no vendor grant applies. Plain SQL runs through
`run_script`.

Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.

Registration should still use the role the task needs: this is the operator's
own registration-time decision in Postgres's own terms, not a plugin gate.
`test_connection` reports the connecting role so a misconfigured credential is
visible.

## Containment — never the platform's own database (§8.4)

The plugin can reach ONLY DSNs that exist as `external_pg::*` address-book
entries (no DSN/URL parameter on any verb). On top of that, `assert_foreign_target`
refuses the platform's own DB **instance** — keyed on `(host, port, dbname)`,
**role-independently**:

- refused: `dbname == SOLET_NAME` **AND** host ∈ {`localhost`, `127.0.0.1`, `::1`,
  the unix socket} **AND** `port == platform_pg_port`. This catches every role
  bound to the platform database — the refusal keys on the instance, not the role.
- allowed: a dev DB on `localhost` with a different dbname, or a same-named DB
  on a different host/port. localhost is a legitimate target *class*.

`_normalize_host` collapses a blank host **and** any absolute-path unix-socket
spelling (`/tmp`, `/var/run/postgresql`) to the socket sentinel, and a blank
port defaults to 5432 — so neither a blank-host nor a blank-port registration can
slip the guard. **Accepted v1 residual:** a non-loopback *alias* that resolves to
the local platform host would slip the literal host check; it requires a contrived
mis-registration, and the registered-entries-only invariant is the primary
containment. DNS/host-resolution
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
(`<solet>.default_address_book_plugin.external_pg_<name>_password`) and is
chain-consumed via `resolve_with_secrets` — this plugin owns no vault keys and
needs no vault binding.

```
process_call service_interface::address_book_service::register {
  "name": "external_pg::analytics",
  "address_type": "database",
  "description": "Analytics RDS",
  "entries": [
    {"field_type": "host",     "value": "analytics.abc123.us-west-2.rds.amazonaws.com"},
    {"field_type": "port",     "value": "5432"},
    {"field_type": "dbname",   "value": "analytics"},
    {"field_type": "user",     "value": "analytics_app"},
    {"field_type": "sslmode",  "value": "require"},
    {"field_type": "password", "value": "vault::<solet>.default_address_book_plugin.external_pg_analytics_password"}
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
  the server's own `transaction_read_only` value (as `read_only`) so the operator
  can confirm a connection points where they expect before trusting it.

## Verb map (all EDGE)

Every verb below except `list_connections` is **born-async** (D0.3
deferred-completion shape, 2026-08-09/10): the dispatch call returns
`{job_id, status: "queued"}` in milliseconds, and the "Returns" column below
describes what the JOB delivers when it completes — NOT this call's own
immediate return value. Conflating "the dispatch returned" with "the job
finished" is the doctrine's named trap; see
`workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md`.

| Verb | Args | Job-completion result |
|---|---|---|
| `run_query` | `connection_name, sql, output_tsv_path, acknowledge_default_limit_override=false, row_limit` | `{path, columns, row_count, truncated}` — written as ONE `.tsv` file at the caller's ABSOLUTE path, never rows inline; defaults to 500 rows, up to 1000 with an acknowledged override |
| `run_statement` | `connection_name, sql, output_tsv_path (only if the statement has a RETURNING clause), acknowledge_default_limit_override=false, row_limit, autocommit=false` | No RETURNING: `{rowcount, has_result_set: false, autocommit}`. RETURNING: `{rowcount, has_result_set: true, autocommit, path, columns, row_count, truncated}` — same always-TSV shape as run_query for the returned rows. `autocommit=true` runs commands the server refuses inside a transaction block (VACUUM, CREATE DATABASE, CREATE INDEX CONCURRENTLY, ALTER SYSTEM) |
| `list_connections` | — | `{connections: [name]}` (names only, never secrets) — still SYNCHRONOUS, not async: a single address-book scan, not a foreign-DB round trip |
| `list_schemas` | `connection_name` | `{schemas: [name]}` |
| `list_tables` | `connection_name, schema` | `{tables: [{name, kind}]}` |
| `describe_table` | `connection_name, schema, table` | `{columns: [{name, type, nullable, default}]}` |
| `export_query` | `connection_name, sql, output_tsv_path, acknowledge_default_limit_override=false, row_limit` | `{path, columns, row_count, truncated}` — the N>>500 route: same shape as run_query, defaults to 500 rows, up to 50000 with an acknowledged override |
| `test_connection` | `connection_name` | `{ok, host, server_version, current_user, read_only}` (`read_only` is the server's own `transaction_read_only`) |
| `run_script` | `connection_name, sql, on_error=rollback, commit=true` | `{statement_count, succeeded_count, failed_count, committed, on_error, statements: [{index, command, status, command_tag, rowcount, has_result_set}]}`; a rollback-mode failure is `external_pg.script_statement_failed` naming the statement |
| `copy` | `connection_name, operation (from_file / to_file), path, schema, table, columns, query, where`, every COPY option | from_file `{operation, path, rowcount, bytes, notices}`; to_file `{operation, path, rowcount, bytes}` |
| `dump_database` | `connection_name, output_path, format=plain`, every pg_dump option | `{output_path, format, bytes, messages}` |
| `restore_database` | `connection_name, input_path, format (optional check)`, every pg_restore restore option | `{input_path, format, tool, messages}` for custom, directory and tar archives through `pg_restore`; a plain SQL file is refused (`external_pg.plain_sql_restore_refused`) and belongs in `run_script` |
| `activity` | `connection_name, operation (list / locks / cancel / terminate), pid, timeout_ms, state, database, granted, limit, offset` | list/locks `{operation, rows, returned, limit, offset, next_offset}`; cancel/terminate `{operation, pid, signalled}` |
| `statistics` | `connection_name, operation (database_sizes / relation_sizes / table_stats / index_stats / reset / reset_table), schema, table, limit, offset` | readers `{operation, rows, returned, limit, offset, next_offset}`; resets `{operation, reset}` |

Every file the plugin reads or writes (`run_query`/`export_query`/`run_statement`
TSVs, `copy` files, dumps and restore inputs) is in the operator's OWN workspace,
never platform blob storage (operator ruling 2026-07-15: bulk business data belongs in workspace
files). The path must be absolute (the TSV verbs also require `.tsv`) and lie under an
saved path address or the session launch folder. The shared file-location guard also includes the solet home and exports folders and excludes the installation tree in both directions. Ask "where will you save this?" to see the selected folder and advisories.

Both `run_query` and `export_query` ALWAYS write their result to the caller's
`output_tsv_path` — never rows inline, at any size (business-data limits +
data-export migration, 2026-08-02; the former inline-return/byte-cap branch
is deleted, not lowered). Each defaults to 500 rows absent an acknowledged
override; `run_query`'s override ceiling is 1000, `export_query`'s is 50000
(the N>>500 route). `acknowledge_default_limit_override=true` together with
an explicit `row_limit` requests more than the default — both are required
together, and a `row_limit` above the verb's hard cap is refused, never
silently clamped. Neither verb has a vendor-imposed ceiling to defer to: this
connection is an arbitrary customer database, so both limits are entirely
our own policy. Columns are written to the `.tsv` header in query order, with
duplicate column names (a JOIN's `SELECT a.id, b.id`) preserved positionally
rather than collapsed. Non-primitive column values (timestamps, Decimals,
UUIDs) are coerced to their string form before writing.

## Dependencies + delivery

`psycopg[binary]>=3.2` (the executor; binary wheel avoids a libpq build dep) and
`sqlparse>=0.5` (the statement splitter). `dump_database` and `restore_database`
also need the PostgreSQL client programs `pg_dump` and `pg_restore` (an
external binary install, not a Python dependency), found in the `client_bin_dir`
config directory, then on `PATH`, then in the standard Homebrew and Debian
PostgreSQL directories; `pg_dump` must be at least the server's major version.
`client_tool_timeout_seconds` (default 3600) bounds one run. Delivered **INERT**: after landing,
the operator runs `pip install -e plugins/external_postgres_plugin` into the shared
`.venv`. Until installed the plugin is dormant tree-ware.

The hermetic smokes run without a database. The three LIVE smokes run against
private scratch databases provisioned by `quality_gates/provision_test_pg.py`
(NEVER the platform database): `smoke_readonly.py` proves that reads run on an
unrestricted connection and that a data-modifying CTE through `run_query`
commits; `smoke_multistatement.py` proves `run_script`'s one transaction,
savepoints and the failing-statement error; `smoke_write.py` proves
`run_statement` and walks the build-1 acceptance path (create a schema, migrate
it, bulk-load a CSV with `copy`, dump it, restore it into a second database,
and read the activity and statistics views).
