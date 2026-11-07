# marketo_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:marketo

Embedding Description: Operator-facing pitch and setup steps for connecting a solet's marketo_plugin to a Marketo Engage instance with OAuth client-credentials (lead query/CRUD including delete, campaign trigger, static list membership), surfaced during hydration if this plugin is present but not yet connected. Includes checking for and, if needed, walking the operator through the API-only Role/User/LaunchPoint setup Marketo requires.

## Pitch

With `marketo_plugin` installed but no instance registered, every Marketo verb
returns `marketo.not_configured` — the plugin is present and harmless, but
does nothing. Connecting it lets the solet read and act on a Marketo
Engage instance directly: leads, companies, opportunities and their lead
roles, sales persons, named accounts and named account lists, custom object
records, campaigns, lists, asset folders and local My Tokens — including permanently
deleting lead records (`delete_leads`) and irreversibly merging lead records
(`merge_leads` — with `merge_in_crm=true`, the merge also reaches and
permanently combines the synced CRM record). Both are genuinely destructive,
and available operations follow the vendor role's permissions. The generic
`request` verb is also available for authenticated relative-path REST operations
under the same vendor RBAC boundary. Ask before doing any of it: "This
solet can read and modify your Marketo instance — leads, campaigns,
lists — including permanently deleting or merging lead records (a merge can
also permanently combine the synced CRM record). That needs a Marketo
API-only user (may already exist) and about 5 minutes in the Marketo admin
UI. Want to set this up now, or later?"

Auth is OAuth client-credentials — the simplest in the connector suite: one
client_id + client_secret pair, no browser consent screen, no callback
server, no token expiry ceremony.

## Setup

Marketo REST access needs three admin-console objects, in order: an
**API-only Role** (specific "Access API" permission checkboxes), an
**API-only User** assigned that Role, and a **LaunchPoint Custom Service**
bound to that user (which mints the client_id/client_secret). **Check first**
— many instances already have this from a prior integration (Bizible, a
Salesforce sync, another marketing-ops tool): ask the operator to open Admin
→ LaunchPoint and say whether there's an existing Custom Service to reuse
before building a new one.

Full step-by-step detail (exact permission checkboxes, console paths, the
standard agent-blind secret-ingestion procedure) lives in this plugin's own
overview article
(`plugins/marketo_plugin/knowledge_base/01_marketo_overview.md`, "Registering
the Role, API-only User, and LaunchPoint service") — this is the condensed
version for the hydration conversation:

1. **New service?** Create a Role with the Access API permissions the work
   requires (including write, delete, merge, or campaign permissions as
   needed) → API-only User → LaunchPoint Custom Service → Get Token. Vendor
   RBAC is the only capability boundary; do not request a read-only role.
2. **Reusing an existing service?** Skip straight to Get Token on it.
3. **Copy** the client secret (the operator's only browser act beyond
   clicking through the above) — the agent harvests it via `pbpaste` into a
   temp file (never displayed), seeds it through `vault_service::
   store_from_file`, then deletes the file and clears the clipboard. Never a
   bespoke seed script.
4. Register the `marketo_instance` address-book entry (`base_url`,
   `client_id`, `client_secret` = the vault reference).
5. If not in the live manifest, add it via blue-green deploy.

**Round-trip count: 2** — "copied" after Get Token, and confirming
new-vs-reuse for the LaunchPoint service. Everything else is the agent's.

## Why this is secure

The client secret never enters model context or logs: clipboard harvest goes
straight to a file the model never reads back, verified only structurally
(byte count), then `store_from_file` puts it server-side into the
OS-encrypted vault under the address book's own namespace — this plugin
never reads the raw secret directly. Temp file deleted and clipboard cleared
immediately after seeding. Honest residual: the clipboard holds it in
cleartext for the few seconds between copy and harvest, and a clipboard
manager or cross-device sync could in principle capture it in that window.

## Verify — `check_setup` is the "check" step

Once `marketo_instance` is registered: run `test_connection` (credentials
valid?) then `check_setup` (what does the Role actually grant?).
`check_setup` runs six safe read probes and names the exact missing
Access API permission for anything that fails, plus which admin screen fixes
it. It **cannot** check write/execute permissions without performing them:
`create_or_update_leads`, `delete_leads`, `merge_leads`, `add_leads_to_list`,
`remove_leads_from_list`, `trigger_campaign`, the `sync_`/`delete_` verbs for
companies, opportunities, opportunity roles, sales persons, named accounts,
named account lists and custom objects, and
`add_`/`remove_named_account_list_members`, `create_asset_folder`,
`update_asset_folder`, `delete_asset_folder`, `set_asset_token` and
`delete_asset_token` are listed as `writes_unverified`
and will surface as `marketo.permission_denied`, naming the gap, on first real
use if the Role is short one. The object read verbs (`describe_`/`get_` for
those objects, `get_named_account_list_members` and `list_custom_object_types`)
are not probed either. They need the Role's Read-Only (or Read-Write) Company,
Opportunity, Sales Person, Named Account and Named Account List permissions,
and the Role's custom-object permission on a subscription that includes
custom objects; the operator confirms these on the Role screen.

The Asset API surface (`/rest/asset/v1/…`: programs, smart campaigns, smart
lists, emails, email templates, landing pages, forms, files, tokens, folders,
tags, channels) is **reachable now** through the generic `request` verb,
including create, clone, update, approve and delete with JSON or form bodies
(file uploads need multipart support that `request` does not have yet); typed
Folder and My Token verbs are now typed: folder browse/by-id/by-name/content,
create/update/archive/unarchive/delete, and token read/set/delete. Other typed
Asset families follow in later builds. What it can do is decided by the Role's **`Read-Only Assets`**
and **`Read-Write Assets`** Access API permissions and by the asset's
workspace. All six `check_setup` probes are Lead-API reads, so a green
`check_setup` says nothing about Asset access. Ask the operator to confirm on
Admin → Users & Roles that the API role carries the Assets permissions the work
needs, in the workspaces it targets; a missing one shows up as the vendor's
`603 Access denied` in the typed Asset job error or the `request` job result. State it as a permission to
verify, not one this setup has confirmed.

On decline: stop, leave the plugin dormant. `marketo.not_configured` on every
verb is the fully-supported steady state, not a broken one.
