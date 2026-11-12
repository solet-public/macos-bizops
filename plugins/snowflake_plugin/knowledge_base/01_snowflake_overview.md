# Snowflake Plugin (`snowflake_plugin`)

A query (and, per the operator's 2026-08-09 posture reversal + Amendment 1,
write) connector over the operator's Snowflake warehouse account.
Executor: `snowflake-connector-python`. Auth: key-pair (RSA JWT) as the
operator's own Snowflake user — no PAT, no browser flow, no callback server
(the platform's durable-non-interactive-auth principle: a solet runs
headless and unattended, so no connector should ever need a human to click
through a login). Most operators already query Snowflake exactly this way
from local scripts — same user and same key pair — and the connector reuses
that setup unchanged, mirroring the Salesforce connector's
ride-the-operator's-own-login pattern.

Single account v1: the "snowflake_account" address-book entry. Per-account
entries (`snowflake::<name>`) are a v2 extension if a second account appears.

## Vendor-controlled access (operator ruling 2026-08-09 + Amendment 1)

The connector forwards SQL under the operator-registered Snowflake role.
Writes and deletes are supported, and Amendment 1 withdrew any plugin-side
write gate ("Don't invent problems to solve — especially ones that the systems
we are connecting to already have solutions built in"). The registered role's
server-side grants are the entire access-control boundary; a denied operation
is reported by Snowflake rather than reimplemented by this plugin.

**The asymmetry with `external_postgres_plugin` matters here.** Postgres has a
session-level `conn.read_only` connection characteristic that is LOAD-BEARING
for its read verbs — it structurally refuses any write at the server, even for
an over-privileged credential — and its write verb flips that characteristic
off per-call. **Snowflake has no equivalent session-level authorization flag at
all**, for either query or statement verbs. So:

1. **The plugin does not classify statements by read/write leader.** Query,
   export, introspection, and `run_statement` paths forward SQL to Snowflake;
   `run_statement` retains its single-statement transaction and result-shape
   contract. Snowflake's vendor authorization decides which operation succeeds.
2. **Grant the registered role the work requires.** `USAGE`, `SELECT`,
   `INSERT`, `UPDATE`, `DELETE`, `MERGE`, and DDL grants are all vendor-side
   choices. `test_connection` surfaces the resolved role, and a denied
   operation remains a Snowflake permission result rather than a plugin policy.
3. **Single-statement is native for every connector verb, including the
   write one.** Multi-statement SQL goes through the `sql_api` verb, which
   sets `MULTI_STATEMENT_COUNT` explicitly in its request body.
   Snowflake's `MULTI_STATEMENT_COUNT` defaults to 1 and this plugin never
   calls `execute_string`, so no statement-splitting parser is needed
   anywhere (unlike Postgres's `sqlparse` belt, which its own `run_statement`
   still reuses as a shape guard) — verified live against the operator's own
   account: a two-statement string submitted through `run_query` was refused
   by the driver itself, not by any plugin code.
4. **`run_statement` manages its own transaction, not a connection
   characteristic.** Snowflake sessions default to `AUTOCOMMIT=TRUE` (every
   other verb here is unaffected by this — a single read statement completes
   or fails on its own either way). `run_statement` explicitly disables
   autocommit on its own connection before executing, so a statement that
   turns out to produce a result set (e.g. a RETURNING clause, where the
   target object supports one) but arrives with no `output_tsv_path` can be
   rolled back instead of silently committing while discarding the returned
   rows — the same always-TSV, never-inline contract every export verb here
   already follows.

**v1 result contract:** scratch/temp writes and other statements are governed
by the registered vendor role and Snowflake's own statement/result behavior.
`run_statement` additionally manages its explicit transaction and requires an
output path whenever a statement produces rows.

## REST API verbs

The same registered account, user and RSA key pair also sign a fresh 60-second
JWT for Snowflake's REST APIs (`rest_client.py`). No extra credential is
registered. `request` is the generic authenticated path. Twelve typed families
wrap it, each one verb with an `operation` parameter per documented endpoint:

| Verb | Vendor API | Operations |
|---|---|---|
| `sql_api` | SQL API `/api/v2/statements` | `submit` (single or multi-statement), `status` (with result `partition`), `cancel` |
| `task` | Task REST API | `list`, `create`, `get`, `create_or_alter`, `delete`, `execute`, `resume`, `suspend`, `dependents`, `current_graphs`, `complete_graphs`, tags, `fetch_result` |
| `stream` | Stream REST API | `list`, `create`, `get`, `delete`, `clone`, tags, `fetch_result` |
| `dynamic_table` | Dynamic Table REST API | `list`, `create`, `get`, `delete`, `clone`, `undrop`, `suspend`, `resume`, `refresh`, recluster actions, `swap_with`, tags, `fetch_result` |
| `database` | Database REST API `/api/v2/databases` | `list`, `create`, `create_from_share`, `clone`, `get`, `create_or_alter`, `delete`, `undrop`, replication (`enable_replication`, `disable_replication`, `refresh_replication`), failover (`enable_failover`, `disable_failover`, `primary_failover`), tags, `fetch_result` |
| `schema` | Schema REST API | `list`, `create`, `clone`, `undrop`, `get`, `create_or_alter`, `delete`, tags, `fetch_result` |
| `warehouse` | Warehouse REST API `/api/v2/warehouses` | `list`, `create`, `get`, `create_or_alter`, `delete`, `resume`, `suspend`, `abort`, `enable`, `disable`, `rename`, tags, `fetch_result` |
| `stage` | Stage REST API | `list`, `create`, `get`, `delete`, `list_files`, `presigned_url`, `fetch_result` |
| `table` | Table REST API | `list`, `create`, `create_as_select`, `create_using_template`, `get`, `create_or_alter`, `delete`, `clone`, `create_like`, `undrop`, `suspend_recluster`, `resume_recluster`, `swap_with`, tags, `fetch_result` |
| `view` | View REST API | `list`, `create`, `get`, `delete`, tags, `fetch_result` |
| `sequence` | Sequence REST API | `list`, `create`, `get`, `delete`, `clone`, `rename`, `fetch_result` |
| `pipe` | Pipe REST API and Snowpipe REST API `/v1/data/pipes/{database}.{schema}.{name}` | `list`, `create`, `get`, `delete`, `refresh`, tags, `insert_files`, `insert_report`, `load_history_scan`, `fetch_result` |

Endpoints the vendor specification marks deprecated (`databases/{name}:from_share`,
`warehouses/{name}:use`, and the underscore table actions such as
`tables/{name}:as_select` and `tables/{name}:swapwith`) are not wrapped; `request`
still reaches them. A pipe `insert_files` success means Snowpipe queued the files,
not that it loaded them: `insert_report` (continued with `next_begin_mark`) and
`load_history_scan` (continued with `next_start_time_inclusive` while
`completeResult` is `false`) report the loads. Stage
file transfer (`PUT`, `GET`), `REMOVE` and `COPY INTO` are connector SQL, not
part of the Stage REST API: `run_statement` forwards them until a typed stage
file verb exists. `presigned_url` returns a time-limited bearer URL for one file,
which is the product the caller asked for.

`rest_families.py` checks typed input against the vendor's published shape
(identifier pattern, documented query names, types and ranges such as
`showLimit` 1 to 10000, `createMode` values, required body members) before
queueing; it never classifies reads against
writes. Results carry HTTP status, typed `error_code`, the body as received, and
a `pagination` hint (statement handles, next partition, `next_from_name`, Link
relations, 202 result handler). The hint is never followed automatically.
Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.

## Connecting the operator's account (one-time)

The connector runs as **the operator's own Snowflake user** under the role
registered for the work. If your local scripts already connect with a key pair
and the required role, reuse exactly that — no new Snowflake objects, steps 1–2
collapse to "point the address-book entry at what you already have."

1. **Account identifier + username (operator does browser acts only).** Ask
   the operator to log into Snowflake in their browser and provide the
   Snowsight address-bar URL plus their username. The URL
   `https://app.snowflake.com/<orgname>/<account_name>` maps to account
   identifier `<orgname>-<account_name>` (driver host
   `<orgname>-<account_name>.snowflakecomputing.com`). The username shows in
   Snowsight's bottom-left profile menu or via `SELECT CURRENT_USER();`.
2. **Key pair — RSA 4096, no smaller** (operator policy, 2026-07-16: every
   platform keypair is 4096-bit minimum). If your user doesn't have key-pair
   auth yet, generate one (the driving agent runs this; agent-blind means the
   key CONTENTS never enter context — verify structurally via
   `wc`/fingerprints/`openssl rsa -noout -text | head -1`, never `cat`):
   ```
   openssl genrsa 4096 | openssl pkcs8 -topk8 -nocrypt -out rsa_key.p8
   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
   ```
3. **Attach + verify via a one-time browser-auth session** (proven live
   2026-07-16, origin -> branchmetrics). The agent runs
   `tools/attach_public_key.py <account> <user>` — it connects with
   `authenticator="externalbrowser"` (the operator approves the login in
   their own browser; SSO/MFA fine; no credential enters agent context),
   executes `ALTER USER <you> SET RSA_PUBLIC_KEY_2='<pub>'` (slot 2, so an
   existing slot-1 key on another machine keeps working), verifies the
   `RSA_PUBLIC_KEY_2_FP` fingerprint against the local
   `openssl ... -pubout -outform DER | sha256` value, and introspects
   `SHOW GRANTS TO USER` / `SHOW WAREHOUSES` / `SHOW DATABASES` so step 5's
   entry values come from real grants. Snowflake permits self-service
   `ALTER USER` on your own RSA key properties; if your org has disabled
   that, fall back to pasting the same `ALTER USER` statement in a
   worksheet (or your admin runs it).
4. **Vendor role** (the load-bearing boundary, §1): use the role you already
   query with, or create one with every permission the work requires. If you
   need to create one:
   ```sql
   CREATE ROLE IF NOT EXISTS <role>;
   GRANT USAGE ON WAREHOUSE <wh> TO ROLE <role>;
   GRANT USAGE ON DATABASE <db> TO ROLE <role>;
   GRANT USAGE ON ALL SCHEMAS IN DATABASE <db> TO ROLE <role>;
   GRANT SELECT ON ALL TABLES IN DATABASE <db> TO ROLE <role>;
   GRANT SELECT ON FUTURE TABLES IN DATABASE <db> TO ROLE <role>;
   -- Add INSERT/UPDATE/DELETE/MERGE/DDL grants required by the work.
   GRANT ROLE <role> TO USER <you>;
   ```
   Personal-account caution: if your user has
   `DEFAULT_SECONDARY_ROLES = ('ALL')`, sessions authorize through ALL your
   granted roles, not just the registered one — unset it so the role
   pin stays the boundary (the step-3 tool prints this property so the
   exposure is visible immediately).
5. **Register the address-book entry** `snowflake_account` with the literal
   fields (`account`, `user=<you>`, `warehouse`, `database`, `schema`,
   `role=<role>`, `auth_method=key_pair`) and a `private_key` field
   holding `vault::<solet>.default_address_book_plugin.snowflake_private_key`.
   Ingest the PEM contents of `rsa_key.p8` into that vault key agent-blind
   via `vault_service::store_from_file` (the exact multi-line PEM text — a
   flattened key fails loud at config resolution, not at first connect; see
   `app_config.py`'s eager PEM parse). Replacing an existing vault key is
   `delete` + `store_from_file` — overwrite is refused (`vault.key_exists`)
   and `rotate` would move the plaintext through agent context.
6. **Zero-downtime key rotation** (when needed): Snowflake supports a second
   key slot — `ALTER USER <you> SET RSA_PUBLIC_KEY_2='<new key>'`, rotate
   the vault secret to the new private key, verify with `test_connection`,
   then `ALTER USER <you> UNSET RSA_PUBLIC_KEY` to drop the old slot.

## Security posture (mirrors the Jira/external-Postgres wave)

- **Foreign-target invariant.** No `account` parameter on any verb — every
  connection is built from the single registered `snowflake_account` entry.
- **No export deny.** Not denied from the MCP surface — call `process_call` on
  these verbs directly. The prior RATIFY-3 deny was retired by operator ruling
  2026-07-15: friction, not security, on a single-user substrate where every
  MCP session is the operator (see
  `workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
- **Generic error messages for topology-leaking classes.** Auth, connection,
  permission, timeout, and warehouse-suspended errors NEVER embed the raw
  driver exception string (it carries account/user/warehouse topology).
  Object-not-found and query-syntax errors describe the caller's own
  query/object and may keep driver detail.
- **Secrets hygiene.** The private key is chain-consumed through the address
  book's `resolve_with_secrets` — this plugin declares no vault keys of its
  own (`get_required_vault_keys`/`get_declared_vault_keys` both return `[]`).

## SQL-lockdown gate note

`snowflake.connector` is not a recognized SQL-driver import root, so the
gate's S0 driver-import class does not fire — no S0 allowlist entry is
needed. The S2 raw-SQL-string class DOES fire on `query_actions.py`'s
`SHOW`/`DESCRIBE`-composed strings and the caller's own `run_query` text —
one whole-file `sanctioned-exempt` entry in `quality_gates/sql_access_allowlist.txt`
covers it (same mechanism as `external_postgres_plugin`'s and
`salesforce_plugin`'s SQL/SOQL-composing modules). `statement_guard.py`
contains no SQL string literals and stays fully gated.

## Key files

| File | Purpose |
|---|---|
| `src/snowflake_plugin/constants.py` | Every magic value: address-book field names, result caps, error codes, and result types. |
| `src/snowflake_plugin/app_config.py` | Resolves `snowflake_account` from the address book; eagerly parses the PEM private key to DER (fail-loud on a flattened key). |
| `src/snowflake_plugin/connection.py` | `snowflake.connector.connect(...)` with key-pair auth; session hardening (`ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS`); topology-safe error classification. |
| `src/snowflake_plugin/statement_guard.py` | Input validation for empty or comment-led SQL before forwarding; vendor RBAC remains the access boundary. |
| `src/snowflake_plugin/query_actions.py` | Pure verb implementations: `run_query`, `run_statement` (the write verb), `list_databases`, `list_schemas`, `list_tables`, `describe_table`, `export_query`, `test_connection`. |
| `src/snowflake_plugin/plugin.py` | The `SnowflakePlugin` EDGE provider — connection lifecycle, error mapping, EDGE registration, D0.3 dispatch. |
| `src/snowflake_plugin/async_jobs.py` | D0.3 deferred-completion machinery: the single lazily-started background worker thread that does the real connect + query/statement I/O for every verb. |
| `plugins/snowflake_plugin/src/snowflake_plugin/rest_client.py` | Account-bound REST transport: per-call RSA JWT, tenant-bound relative paths, no redirects or retries. |
| `plugins/snowflake_plugin/src/snowflake_plugin/outward_values.py` | JSON-compatible outward forms of native stage-file keys and values (hex binary, text Decimal/date/time, arrays for tuples). |
| `plugins/snowflake_plugin/src/snowflake_plugin/rest_families.py` | Typed `sql_api`, `task`, `stream`, `dynamic_table`, `database`, `schema`, `warehouse`, `stage`, `table`, `view`, `sequence` and `pipe` operation tables, input checks, and pagination hints. |
| `src/snowflake_plugin/completion_templates.py` | Builds the `completion_handlers` block each async job's metadata carries, routing job completion back into the originating flow. |

## Stage files (build 5)

`stage_files` is the typed connector family for `put`, `get`, `list`, `remove`,
`copy_into_table`, `copy_into_location` and `copy_files`. It runs on the same
single async worker as other verbs. The queued envelope is separate from the
vendor outcome; inspect every file status and the COPY result TSV. No automatic
retry or rollback is promised: transfers and COPY/REMOVE can have partial effects.

Supply `stage_location` without SQL string quotes, for example `@DB.PUBLIC.FILES/path/`,
`@%TABLE/path/` or `@~/path/`. PUT and GET apply to internal stages, as the vendor
requires. COPY INTO supports internal/external stages and cloud locations with `s3://`,
`s3china://`, `s3gov://`, `gcs://` or `azure://` protocols. Snowflake validates
region/account support. COPY FILES targets a named internal/external stage and
reads a named stage or a query of file URLs (including scoped/stage URLs);
cloud storage is configured on those stages.
PUT takes an absolute local filename or wildcard; every realpath-resolved match
must be under a saved location. GET takes an existing absolute directory
under those same roots. It downloads into an isolated directory before replacing
matching destination entries; an existing destination symlink is replaced, never
followed. GET does not decompress files. For a byte-equality round trip, PUT with
`AUTO_COMPRESS=false` and `SOURCE_COMPRESSION='NONE'`, then GET the same file.
PUT with `OVERWRITE=true` corrects/replaces a file, false retains it as SKIPPED;
REMOVE retires files, and PUT recreates them. LIST returns every vendor row.
There is no file history, close/reopen state or optimistic-concurrency token in
these vendor commands; correction is overwrite and concurrency follows Snowflake.
Use separate versioned stage prefixes when preserving old bytes matters.

`options` accepts SQL option names case insensitively. PUT supports `PARALLEL`
(1–99), `AUTO_COMPRESS`, `SOURCE_COMPRESSION` and `OVERWRITE`; GET supports
`PARALLEL` (1–99) and `PATTERN`; LIST/REMOVE support `PATTERN`. Omitted options
retain Snowflake defaults. COPY accepts every option name, with vendor validation
of types/combinations: strings become SQL literals, booleans/numbers remain
literals, lists become SQL lists, and nested objects become option groups.
For example `{"FILE_FORMAT":{"TYPE":"CSV","SKIP_HEADER":1},"FORCE":true}`.
Use `{"sql":"expression"}` for an identifier or SQL expression, including
`STORAGE_INTEGRATION`, nested `INCLUDE_METADATA` column assignments, or SQL NULL.
`query` supplies transformation/unload/COPY FILES query text without parentheses;
`columns` maps COPY INTO table targets; `partition_by` supplies an unload
expression. Partition values appear in filenames and Snowflake logs, so choose
nonsensitive date/time/boolean values as the vendor recommends.

Both COPY INTO forms require `output_tsv_path`: every result row, including
validation/RETURN_ROWS business data, goes to that contained TSV. COPY FILES and
transfer/list/remove results contain file metadata inline. Every key and value
first becomes JSON-compatible: binary buffers use `hex:<lowercase hex>`
strings, Decimal/date/time use text, and tuples use arrays (tuple keys use
compact JSON strings). SQL NULL remains an empty TSV cell. Status/success and
counts are computed from the rows as received. Results are not scanned. The connector returns the vendor result as received;
bulk results go to disk through `output_path`. Audit and log lines are built
only from method, path without query values, status, duration and correlation
id, so they never carry headers, bodies or credential values.
The input job payload holds the request needed for execution, like the
existing SQL/REST jobs; never put its credential-bearing contents in a
user-facing transcript.

`stage_sql.py` builds commands, `stage_paths.py` applies local containment, and
`stage_files.py` runs commands and maps results. The registered offline smoke
`plugins/snowflake_plugin/tests/smoke_stage_files.py` proves binary round trip, correction/removal/recreation,
containment, COPY shapes and error mapping using
a connector-shaped fake. It does not measure a live Snowflake account.
