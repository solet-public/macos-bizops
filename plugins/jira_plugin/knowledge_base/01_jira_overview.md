# Jira Cloud Connector (`jira_plugin`)

`jira_plugin` gives the solet, peers, and plans read/write access to a Jira
Cloud site — issues, JQL search, comments, workflow transitions, and attachments.
It is Lane C of the Enterprise Connector Suite and forks the shape of the
`g_suite_plugin` reference vertical (sync `(params, state)` verbs over pure action
modules, an injected blob writer/loader, a `_run` error classifier, per-verb EDGE
definitions). Legacy verbs use pycontribs `jira`; the new typed v3/Agile families use a pinned
vendor operation inventory and the standard-library JSON transport. See
[vendor API coverage](vendor_api_coverage.md) for current coverage and later builds.

## Posture at a glance

- **Auth: API tokens and Atlassian OAuth 2.0 (3LO), with multiple sites.**
  The original address-book entry remains `site=legacy`. Bind additional sites
  with `sites`; consent and rotating refresh use `oauth`. Every verb accepts
  optional `site`, uses the configured default when omitted, and names the site
  in queued and completed results. Unknown aliases return `jira.unknown_site`.
- **Full read/write including `delete_issue`.** Jira ticket deletion is an
  acceptable-loss class (unlike database destruction), so v1 ships the complete
  CRUD surface. Destructive verbs take an explicit target (the issue key is
  required; there is no bulk form).
- **The API token EXPIRES.** Atlassian API tokens expire (default one year,
  configurable 1–365 days) and their scopes are fixed at creation. The plugin is
  expiry-aware and warns loudly before the token lapses (see *Token rotation*).
- **No export deny.** Not denied from the MCP surface — call `process_call` on
  these verbs directly. The prior RATIFY-3 deny was retired by operator ruling
  2026-07-15: friction, not security, on a single-user substrate where every
  MCP session is the operator (see
  `workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
- **Attachments are blob-only.** No verb accepts a local filesystem path.

## Credentials — the `jira_site` address-book entry

The plugin resolves one address-book entry named `jira_site` via
`resolve_with_secrets`. It mixes literal fields with one vault-referenced secret:

| field_type | kind | example |
|---|---|---|
| `base_url` | literal | `https://your-org.atlassian.net` |
| `email` | literal | the service account's email |
| `api_token` | `vault::<solet>.default_address_book_plugin.jira_api_token` | chain-consumed |
| `expires_at` | literal (ISO-8601) | the token's expiry, e.g. `2027-01-15T00:00:00Z` |
| `scope_note` | literal | the fixed-at-creation scope, for operator reference |

The legacy `api_token` is chain-consumed in the address-book resolver's vault
namespace. Additional bindings copy each resolved token into its own plugin
vault envelope; OAuth grants also use per-site envelopes. No key is required
at readiness, and runtime key patterns are declared by the plugin.
`expires_at` is validated as ISO-8601 at config-load. API-token base_url may be
the site origin or Atlassian's `https://api.atlassian.com/ex/jira/{cloudId}`
gateway for scoped tokens. OAuth consent uses the site origin and verifies the
gateway cloud ID from accessible resources.

### Service account vs personal account

v1 targets a **dedicated scoped Atlassian service account** so the token's
authority is bounded and its rotation does not disrupt a human's access. A
personal-account API token is a documented drop-in fallback if service-account
seat licensing is inconvenient — the transport (`basic_auth=(email, token)`) and
the `jira_site` entry shape are identical; only the account provenance differs.

### Registration runbook (operator, one time)

1. In the Atlassian account/console, create the service account and grant it the
   project roles it needs (browse, create, edit, transition, delete as
   appropriate). Mint an API token; note its expiry.
2. Ingest the token agent-blind into the resolver's vault namespace under
   `<solet>.default_address_book_plugin.jira_api_token`.
3. Register the `jira_site` address-book entry with `base_url`, `email`,
   `api_token` (the `vault::…` reference), `expires_at`, and `scope_note`.
4. Verify with `test_connection` — it echoes the resolved `base_url` and the
   authenticated account so you can confirm the plugin points where you expect.

### Token rotation runbook (operator, on/before expiry)

The plugin **never mints or rotates the token** — it cannot. Before the recorded
`expires_at`, the plugin logs a loud `jira.token_expiring` warning at client
build (within a configurable `token_expiry_warn_days` window, default 14). On
that signal:

1. Mint a fresh API token in the Atlassian console with the same scope.
2. Agent-blind vault re-store of `jira_api_token`.
3. Update `expires_at` in the `jira_site` entry.

Handled this way, an impending lapse surfaces as a clear warning rather than a
mystery 401.

## Security invariants (enforced in code + smoke)

1. **Foreign-target only.** The plugin reaches only the site resolved from the
   `jira_site` entry — no `base_url`/site parameter on any verb. It never imports
   or touches the platform state database. (This is the honesty condition for the
   suite's SQL-lockdown treatment; Jira is gate-silent — JQL is not SQL.)
2. **No topology leaks.** Connection/auth/permission error messages are generic
   fixed strings; the plugin never returns the driver exception string, because
   `str(JIRAError)` embeds the request URL (the site host). Detail-allowed classes
   (not-found, bad-request) build their message from the Jira response body only,
   which describes the caller's own query/object, not our host.
3. **Blob-only attachment ingest.** `add_attachment` takes a `blob_key` only;
   there is no local-path parameter (a local-file read on a verb that writes to a
   corporate ticket would be a secret-exfiltration primitive). `download_attachment`
   returns an `attachment_blob_key`.
4. **No export deny.** Every verb is directly `process_call`-able; see the
   Posture note above.

## Verb map

| Verb | R/W | Purpose |
|---|---|---|
| `jql_search` | R | Find issues by JQL; returns trimmed rows inline, one complete result up to the row limit |
| `get_issue` | R | One issue's summary, description, people, labels, attachment metadata |
| `create_issue` | W | Open a new issue (project + type + summary + optional fields) |
| `update_issue` | W | Apply a fields object to an existing issue |
| `delete_issue` | W | Permanently delete an issue (explicit key) |
| `add_comment` | W | Post a plain-text comment |
| `list_comments` | R | Read an issue's comment thread; returns rows inline, one complete result up to the row limit |
| `list_transitions` | R | Discover valid workflow moves from the current status |
| `transition_issue` | W | Move an issue through a transition (optional comment) |
| `download_attachment` | R | Fetch an attachment's bytes into blob storage |
| `add_attachment` | W | Attach bytes from a blob to an issue |
| `test_connection` | R | Health check: confirm auth + resolved site/account |

## Business-data limits migration (2026-08-02, revised 2026-08-03)

**Jira EXITED the data-export requirement entirely** — operator veto, 2026-08-03
verbatim: *"We have no PII in Jira - just company internal accounts. Don't
worry at all about PII in Jira."* An earlier revision of this section (still
visible in git history) had `jql_search`/`list_comments` always writing to a
caller-supplied `output_tsv_path` via a workspace-root containment gate
(the shared file-location guard). That gate, the `ExportPathRefusedError` it raised,
and the `jira.export_path_refused` error code are all **deleted**, not
retained as dead code — jira is g_suite-class: results return **inline**,
never to a file
(`workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md`
§0.1/§5.4).

**Paging is hidden — a second 2026-08-03 operator ruling** (verbatim: *"we
need to deliver the results - the paging is an implementation detail that
should be hidden"*) retired the disclosure-only shape both verbs briefly had
after the first revision. `jql_search` and `list_comments` now carry the full
`acknowledge_default_limit_override`/`row_limit` mechanism (§5): each defaults
to a 500-row effective limit and, within that limit, pages **internally**
across Atlassian's 100-issues/100-comments-per-call ceiling — the caller never
sees `next_page_token` or `start_at`. An acknowledged override
(`acknowledge_default_limit_override=true` + `row_limit`) raises the limit up
to a 5,000-row hard cap (refused above that, never silently clamped). Latency
is real and scales with the limit: 5 internal HTTP calls at the 500-row
default, 50 at the 5,000-row cap, each a sequential round-trip — a
defense-in-depth circuit breaker (`MAX_INTERNAL_CALLS=100`) bounds the loop
regardless. If the vendor genuinely has more than the effective limit,
`truncated` is `true` (and on `jql_search`, `total` becomes an approximate
count) — there is no resume token; the over-limit route is narrowing the
query or raising `row_limit`, never external continuation.

**`get_issue` is unaffected — single-record reads stay inline-capable.**
Operator-confirmed 2026-08-02: single-item/single-record reads for
validation purposes are not the mass-exposure risk this migration targets
(only the bulk read surfaces `jql_search`/`list_comments` are in scope).

## Notes

- **REST API v2** is pinned (plain-text description and comment bodies). v3 uses
  Atlassian Document Format (ADF) JSON, which would leak ADF structure into verb
  parameters.
- **`jira.bad_request`** is the general code for an HTTP 400. On `jql_search` this
  almost always means malformed JQL (unknown field, bad operator, unbalanced
  quotes); that guidance lives in the `jql_search` process definition.
- **Delivery is INERT** until the operator installs the plugin
  (`pip install -e plugins/jira_plugin`) into the shared virtualenv and provisions
  the service account + token. The smokes are hermetic and run without a live Jira.


## Multi-site and OAuth lifecycles

`sites(operation=list)` returns the current `version`, default and bound sites.
To add or correct an API-token binding, register a separate address-book entry
with the same five fields as `jira_site`, using its own vaulted token, then call
`sites(operation=bind_api_token, site="team", entry_name="jira_team",
expected_version=<current>, set_default=true)`. Credentials are copied into a
per-site plugin-owned vault envelope; later address-book changes take effect
only after an explicit rebind. The legacy entry stays available independently.

Use `set_default` with `site=legacy` to clear an additional default. Rebind an
existing alias to correct/rotate its credentials. `disconnect` removes the local
binding and its current/retired credential envelopes and pending consent;
Atlassian token/grant revocation remains the vendor account's operation. If a
vault deletion fails after unbinding, the result reports the failure and the
index exposes `credential_cleanup_pending`; repeat disconnect at the new
version to finish cleanup. A failed index write never publishes new credentials
over an existing binding. `sites(operation=history)` reads append-only,
non-secret binding events; vault-native auditing separately records secret
stores, rotations and deletes. Historical secret values are never returned.
Stale expected versions fail before a binding changes. Writes are serialized
within the one plugin instance; the vault does not offer a cross-process CAS.

For OAuth, use a registered Atlassian 3LO application. Its address-book entry
(default `jira_oauth_app`) contains literal `client_id`, registered
`redirect_uri`, space-separated `scopes` including `offline_access`, and a
vault-referenced `client_secret`. Call `oauth(operation=begin, site="team",
base_url="https://your-team.atlassian.net")`, follow the returned consent URL,
and capture the authorization code at the registered redirect into a
plugin-owned `<solet>.jira_plugin.oauth_code...` vault key agent-blind. Complete
with `site`, `state`, `code_vault_key`, current `expected_version`, and optional
`set_default`. The plugin verifies state/ten-minute expiry and the grant's
accessible resources before storing credentials. It uses the granted cloud ID
at `api.atlassian.com/ex/jira/{cloudId}` and persists both rotating access and
refresh tokens. No local callback listener or setup-flow change is introduced.
`status` reads pending consent; `cancel` clears it; `begin` corrects/replaces it.
A consumed or expired state cannot complete again. Auth failures require fresh
consent or credential correction, without exposing vendor response secrets.

## Typed planning families

`projects`, `boards`, `sprints`, `backlog`, `agile_issues`, `versions`, and
`worklogs` select a vendor `operation` and take `path`, `query`, `body`, optional
`body_json` (for arbitrary JSON property values), `headers`, and `site`.
The inventory enumerates every operation in those seven vendor groups, including
writes, deletes, project archive/restore, sprint start/close, property set/clear,
version release/undo, and worklog correction/removal. Vendor-owned objects use
vendor permissions/concurrency/history; no invented local version is sent.

For example, `sprints` createSprint takes body name/originBoardId and optional
goal/startDate/endDate; moveIssuesToSprintAndRank takes path sprintId and body
issues plus rankBeforeIssue/rankAfterIssue/rankCustomFieldId. Rank independently
with `agile_issues` rankIssues. Log time with `worklogs` addWorklog and release a
version with `versions` updateVersion, body released=true. V3 rich-text fields
use the vendor Atlassian Document Format. All documented optional query/body
fields are forwarded, and responses retain native pagination metadata.
`next_query` gives another page's query; queued acceptance is not vendor success.
Use the per-family process articles for operation names and the pinned request
inventory for required path/query/body fields. Writes are never retried blindly.

Queued jobs for additional sites pin the binding identity. A subsequent rebind
returns `jira.binding_changed` before any vendor request; inspect the target
and submit intentionally again. Changing only the default does not retarget
already queued work. The credential envelope can refresh in place without
changing the binding identity. This is a target-integrity check: a stable
binding is the pass condition, the running plugin preserves it until an
explicit rebind/disconnect, and the queue/rebind transcript tests both paths.
