# Model Dispatch Policy

Tags: knowledge:tag:plugin_reference, knowledge:tag:dispatch_policy, knowledge:tag:model_assignment

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:agent-to-agent-coordination, evidence-category:operator-ruling, domain:agent-messaging, domain:managed-dispatch

Embedding Description: Current open dispatch-kind provenance, scope tags, selector receipt replay, retired state-schema model floor, and separation of Main-seat launcher choice from work-unit selection.

## Current contract

The source is `plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`
(`schema_version: 2`, `policy_version: model-dispatch-policy-v3`). It is read for each managed spawn. The historical `.v1.json` filename remains the consumer path. The schema contains only version, dispatch-kind provenance examples, and capability floors; the retired `orchestrator` field or any other extra top-level field refuses policy loading.
Its `dispatch_kinds` section is an empty object because dispatch kinds are
open provenance text, not a model-pair assignment map. The policy parser checks
its shape and rejects nonempty rule objects; it does not restrict callers to
keys in that section. Project Solet's `UnitDispatch.__post_init__` accepts
nonblank unit-kind text. All 16 kind values observed in the live register on
2026-09-24, including `test`, `implement`, `implementation`, `integration`,
`smoke-test`, and `build`, therefore pass as provenance. An additional
nonblank phase minted later also passes without a policy edit. Blank or
non-string values refuse; no case folding, aliasing, or score change occurs.

`spawn_session` and `dispatch_managed_work` require `dispatch_kind`.
`select_dispatch_tier` may omit it; when supplied, it uses the same nonblank
text rule. `reviewed_report_vendor` and `pair_id` are optional provenance
fields. A selector receipt records the required score, billing objective,
selected runtime/model/effort, policy version, and applied floors. Managed
spawn replays an enforced receipt for the exact tuple.

`select_dispatch_tier` also takes an optional `runtime` (`claude_code` or
`codex`). A coordinator that has chosen the runtime passes it so the cheapest
cell of the other runtime cannot dominate its choice out: the catalog is
filtered to that runtime before domination and selection, and the selector
stays cheapest-within-runtime with no runtime preference of its own. The
receipt carries the value as `runtime_constraint` (null when absent), replay
re-applies it, and a receipt without that key is refused; re-select to get one.

Allowance pools are independent telemetry. Record/read/retract verbs retain
operator readings, expiry and pool identity. No exhausted, unknown, missing,
expired or unreadable allowance reading affects selection, pins, cost ranking
or receipt replay. Selection does not read these records and carries no quota
admission, quota advisory or expired-reading fields.

## Model catalog policy and source lifecycle

The catalog's measured policy and provider-source configuration are separate
versioned records. Use `read_model_catalog_policy` for ordinary readback; it
requires `kind` (`policy` or `source`) and a stable `stable_id`, needs no
Operator role, and changes no state. Its `current` result is the complete
typed snapshot, including provenance and `lifecycle_state` (`active` or
`retired`). An absent record is `current: null` with an advisory warning, not
an implicit configuration or numeric default. Optional values that were never
set or were cleared remain `null`.

For immutable history, pass an explicit positive `history_limit`. The response
contains a page ordered by revision and row id and a `next_after` frontier;
continue by passing that exact `[revision, id]` as `after` with the same
`history_limit`. Omitting `history_limit` requests current only and returns
`history` and `next_after` as `null`. History snapshots retain `revision_id`,
`expected_revision`, `predecessor_revision_id`, server-derived `actor`,
`reason`, `action`, `recorded_at`, and `lifecycle_state`.

The Operator-only writers are `set_model_catalog_policy` and
`set_model_catalog_source`. Both require `stable_id`, `action`,
`expected_revision`, and a nonblank `reason`. An initial `set` creates an
absent record with an explicit `expected_revision: null`; existing-record
actions use the positive revision returned by `read_model_catalog_policy`.
`correct` changes supplied values, `clear` takes a nonempty `clear_fields`
list, `retire` transitions active to retired, and `reopen` transitions retired
to active. Only `set` and `correct` accept `values`, and values omit nulls.
Correct and clear preserve the lifecycle state. Each successful result is the
new complete snapshot with its revision and predecessor, and the matching
immutable history snapshot is committed atomically. A stale premise or
history failure rolls back the whole change; retirement is reversible and
history is retained.

Policy `values` may set or correct `dispatch_score_margin`,
`cost_tie_tolerance`, `confidence_level`, `interval_method`,
`interval_method_revision`, `bootstrap_resamples`, `outcome_window_hours`,
`staleness_window_hours`, `refresh_interval_seconds`, and
`refresh_dedupe_seconds`. `clear_fields` may name any of those fields.
Numerical values must be finite; probabilities stay strictly between their
endpoints, counts and windows/cadences are positive, and margins, tolerances,
and deduplication values are nonnegative. Method names and revisions are
nonblank strings. Clearing removes the value and supplies no replacement
default. This policy configuration does not invoke `set_model_cells_enabled`
and does not change cell flags, acceptance, or eligibility.

Source `values` initially require `provider_id`, `source_id`,
`implementation_id`, `implementation_version`, `metric_units`, `aliases`,
and strict-boolean `enabled`. Provider, source, implementation, and version
identifiers are nonblank strings; `metric_units` and `aliases` are
string-to-string objects. Correct may change these fields, and empty objects
clear `metric_units` or `aliases`. Optional `endpoint`, `credential_ref`, and
`priority` support set/correct/clear; `priority` is nonnegative. An endpoint
is nonblank and contains no user information, authentication, query string,
or fragment. `credential_ref` is a nonblank credential-service reference:
store and expose the reference only, never a secret, and do not resolve it in
this process. Registration `enabled` and lifecycle state remain independent
of model-cell enablement. Read source current/history with
`read_model_catalog_policy` using `kind: "source"` and the same stable id.

These processes do not fetch networks, apply schemas, resolve credentials,
deploy, or manage sessions. The server derives the current Operator identity;
callers must not supply `actor`, `agent_session_id`, or `agent_instance_id`.
On `parameter_invalid`, correct the action, kind, stable id, reason, typed
values/clear fields, revision, or pagination frontier. On
`stale_catalog_revision`, read current state again and make a new decision.
On `invalid_transition`, read `lifecycle_state` before choosing retire or
reopen. `operator_required` means invoke from the currently bound Operator;
`operator_identity_spoofed` means remove caller identity fields.
`state_service_unavailable` means restore the state-service binding and retry
after checking current state. These errors never justify inventing defaults or
partially applying a revision.

## Operator cell enablement

Every catalog cell has `enabled=true` by default, independently of acceptance.
`select_dispatch_tier` applies this filter once before pins, constraints,
frontier and ladders. Disabled cells remain visible in catalog and policy reads.
Existing receipt replay uses the same filter; disabling a selected cell makes
its previous receipt fail replay. Re-enable restores eligibility and retains
scores and acceptance. Seed, refresh, harness withdraw/reinstate and manual
recording preserve existing flags. New cells evaluate the standing policy.

The current Operator calls `read_model_cell_policy` first, then passes its
`updated_at` as `expected_updated_at` to `set_model_cells_enabled`, with a
nonempty reason. Only the initial uninitialized policy accepts an explicit null
premise. The wrapper derives session identity from server state and checks the
current Operator binding on each call; caller actor strings confer no authority.

Examples below are verb parameters (substitute the latest premise for `STAMP`):

```json
{"selector":{"model":"claude-opus-5-5"},"enabled":false,"expected_updated_at":"STAMP","reason":"disable Opus"}
{"selector":{"provider":"anthropic","runtime":"claude_code","model_family_prefix":"claude-sonnet","effort":"high"},"enabled":false,"expected_updated_at":"STAMP","reason":"disable matching high efforts"}
{"selector":{"model_family_prefix":"claude-sonnet"},"enabled":true,"only":true,"expected_updated_at":"STAMP","reason":"only Sonnet"}
{"action":"correct","rule_id":"RULE","selector":{"model":"claude-opus-5-5"},"enabled":true,"expected_updated_at":"STAMP","reason":"re-enable"}
{"action":"retire","rule_id":"RULE","expected_updated_at":"STAMP","reason":"remove standing rule"}
{"action":"clear","expected_updated_at":"STAMP","reason":"restore mixed fleet"}
```

Dimensions combine with AND; absent dimensions are wildcards. Ordered rules
use the last matching decision, with default-on. A zero-match rule is retained
for future models and produces an advisory. Normal set adds a rule and preserves
unrelated rules; correct replaces an identified rule, retire/clear with a rule
id removes it. Clear without a rule id restores all-on. The only form atomically
replaces policy with default-off plus matching-on; it has no visible intermediate
state. Policy, cell flags and existing audit rows commit or roll back together.
Stale premises refuse without partial writes. Malformed stored policy is an
integrity error. Current readback includes rule identifiers, cell provenance,
and bounded immutable policy_change history (history_limit 1..100); per-cell
history includes enabled_change observations. Immutable history is never cleared.

## Where the catalog's models come from: discovery, the harness pass and degraded mode

The catalog learns which models exist from the providers' own lists, not from a table kept
in this repository (operator ruling `rul_473f772f`). A refresh reads each runtime's harness
list: for Codex the listing the launching binary itself prints, for Claude Code the local
model cache. The harness list decides existence, the exact launch id and the offered efforts.
The Artificial Analysis leaderboard supplies score and cost only, and each model's detail page
is the second source for them. A new model therefore needs no code change: once a harness
lists it and the leaderboard scores it, the next refresh creates its cells (at most 30 new
cells per run, none once 300 live rows exist; the rest wait and are taken on later runs).

**What a harness list may prove.** A list is `ok` when it was read from its authoritative
source and offers models. A Codex list read from the shared cache file is `degraded`, a list
that could not be read is `unreadable`, and a list that offers no model is `empty`: a harness
that offers none cannot launch anything, so zero is a read fault, not a fact. An `ok` list
older than seven days proves nothing either. Only a current `ok` list can start, advance or
clear a clock or withdraw a cell; every other list changes none of that, and the refresh says
so in its advisories instead of refusing.

**The harness pass.** Before each reconcile the refresh looks at every cell of a runtime
whose list is current and `ok`. A cell whose model the list does not list starts a clock,
`harness_unverified_since`. After one staleness window of continuous absence (72 hours by
default; the cell's own window if it carries one) the refresh withdraws the cell: the cell
keeps its score, cost and measured time, drops out of selection under the `not_accepted`
count, and one observation records the move. When the model is listed again the clock clears
and a cell the refresh withdrew is reinstated with its retained values. A withdrawal made by
a person (`withdraw_model_capability_cell`, which clears the clock) is never reversed by a
refresh: a withdrawn cell with a null clock stays withdrawn until
`reinstate_model_capability_cell`. A refresh never changes the acceptance of a withdrawn cell
at all; it may record readings and refresh the retained values of one, bounded by the usual
tolerance, and it says so. Reinstating by hand a cell the refresh withdrew is undone by the
next refresh while the model is still unlisted and the window has passed; the reinstate answer
says whether that will happen.

**A model the leaderboard dropped.** A harness-listed model with no leaderboard row keeps its
last score and stays accepted. It is never withdrawn for that reason; each refresh names it
under `unrenewable_stale`, and selection reports the age of its reading as a stale reading.

**Degraded mode.** When a runtime's list is unreadable or empty, leaderboard rows are joined
to the launch ids the catalog already holds, and a row that joins to none becomes a new cell
only if the runtime's launch-id shape accepts the id derived from the leaderboard name and
both the leaderboard and the detail page agree on the score. Such a cell is flagged in its
note and starts its clock when it is created, so an id that no `ok` list ever confirms is
withdrawn one window later. Renewing existing cells is never refused in degraded mode.

## Flat-rate ranking: dispatch weights (iss_eef0812b)

Under a flat-rate plan the plan, not the API meter, is what is charged, yet
`relative` cost is only metered dollars divided by one catalog-wide constant, so
it ranks exactly as `metered_usd` does. A flat-rate profile may therefore declare
`dispatch_weights`: a `by_model_prefix` table, a mandatory `default_weight`, a
`basis` (`operator_policy` with a `ruling_id`, or `measured` with an
`evidence_ref`) and `declared_at`. The longest matching prefix wins. A weight is
an operator-declared dispatch preference, never a token or allowance conversion,
and the provider publishes none. The shipped Max 20x profile declares
`claude-sonnet` at 0.5 under `rul_29449a0b-3b9e-4251-bf87-4d295269a409`
(decision `dec_838f872f-af5f-46a5-abcd-7b8619d6e4d2`), default 1.0. The value is
a policy choice that counts Sonnet's lower list price twice on purpose; whether
Sonnet usage also draws the all-model pool is an unverified assumption.

The objective `allowance_weighted` ranks by `relative_cost_multiplier * weight`.
It is the default only when `runtime` is passed, a current flat-rate plan covers
it and that plan declares weights. It is plan-scoped: an explicit
`allowance_weighted` without such a runtime is `parameter_invalid`, and
unconstrained or Codex selections are never weighted, so allowance units are not
compared with dollars. A covering plan with no table keeps the metered-dollar
ranking and the answer carries a `warnings` line, which is not a refusal. The
answer's `cost_basis` names the plan, the table and the weight applied, and
`frontier`, `dominated` and `ladder` are in weighted units. The receipt keeps its
keys; `billing_objective` records the ranking, and replay recomputes it from the
profile, so editing a weight makes an in-flight receipt mismatch until the
dispatcher re-selects. Operator enablement and capability floors filter cells before ranking.

Two refusals belong to the weighted path. With a runtime constraint the profile
is loaded to read its weights, so an unloadable or malformed profile (a bad
`dispatch_weights` table included) refuses `usage_economics_invalid`; an
unconstrained selection with the same fault does not refuse: an explicit metered objective needs no quota telemetry. And when two current flat-rate plans cover the
same runtime the weights would be ambiguous, so the selection also refuses
`usage_economics_invalid` rather than choosing one.

## Schema scope provenance

`scope_tags: ["state_schema"]` records that a dispatch touches state schema.
It is accepted as nonblank provenance and does not constrain the model pair.
The `state_schema` capability floor was retired under operator ruling
`rul_0c6ec7c7-70c6-442a-9695-3738efb61ccd`: `capability_floors` is now
empty, and schema identifiers in a brief do not apply a model-choice gate.
Selectors still require a fresh accepted catalog cell that clears the numeric
difficulty score. Policy loading requires a `capability_floors` object and
validates any configured future floor, but accepts an empty object and refuses
an attempted restoration of the retired `state_schema` model floor.

The ruling's remedy is a state-documentation link and mechanically assembled
linked information in each schema-touching work-unit brief. This policy change
does not implement brief assembly; `iss_25f79570-9b0d-48e7-a73f-e3a56242b181`
tracks retirement of the model floor in this candidate.

The landing companion `quality_gates/schema_init_gate.py` boots the real
`initialize_schemas` path against a candidate tree. A successful dispatch
selection does not replace that landing gate.

## Main-seat hook transition and failures

The Main seat chooses and configures its launcher model separately from the
work-unit selector, under operator-present seat authority. The fixed Claude
Main-seat model list is retired. The Claude Code project settings file (.claude/settings.json) no longer registers
`orchestrator_model_gate.py` for `PreToolUse` or `SessionStart`. That script is
temporarily callable as a no-op for active Claude processes retaining a cached
hook command; it makes no model or role-authorization decision. After relevant
seats confirm settings reload, a separate cleanup can remove the script and its
registered transition smoke. The server still authenticates managed-dispatch
role authority and replays selection receipts before host dispatch.

The dispatch ledger records kind, declared scope tags, and applied floors
(currently empty) with source and detail. A missing or blank spawn kind returns
`dispatch_kind_required`; a non-string kind returns
`dispatch_policy_violation`. Missing or malformed policy/profile data returns
`dispatch_policy_unavailable` or `dispatch_policy_invalid`. A blank or
non-string scope tag returns `scope_tag_invalid`. A forged, absent, or stale
selection receipt and a caller without the held coordinator role retain their
server-side refusals.

## Usage and the two meters

For the operator-facing extension contract, source coverage, lifecycle
readback, and fixture/provider extension guidance, see [Usage Meters and the
Extension Contract](16_usage_meters_and_extension_contract.md). This article
defines the dispatch-plugin behavior; the companion explains how to interpret
its readings without turning allowance percentages into token or dollar usage.

`read_unit_usage` joins every managed session for one `unit_id`, or one
`agent_instance_id`, to the runtime ids in `session_claude_mapping` plus that
worker's current `session_context_status`. It asks the session ledger for S1
totals in bounded groups of at most 200 ids. `coverage` is `full` only when
every managed worker has a runtime id and every id has complete usage from a
supported meter or a measured zero from a supported meter. Missing mappings,
unresolved ids, unsupported meters, partial reads, and bounded reads remain
visible in `notes` and make usable results `partial`; with no usable supported
usage or confirmed zero, coverage is `none`. Missing runtime data never blocks
the read.

The first meter is actual token usage reported by the harness adapter and
deduplicated by S1. The second is an estimated USD price computed by S2 from
the current metered profile. `observed` contains the ledger's per-turn model,
current context effort only when its runtime id matches, the provider and
runtime used for pricing, `priced_usd`, and a `pricing_basis`. A missing,
ambiguous, or unknown profile yields a null USD and an `unpriced:` basis; it
does not change the token totals. Allowance pools show the latest reading and
whether it has expired. `executor_mismatch` compares observed model/effort with
the managed session's `dispatch_model` and `dispatch_effort`; it never fills
missing observed values from those dispatch fields. An observed `unknown`
model stays `unknown`, is counted in notes, is not a mismatch, and is priced as
`unpriced:model_unknown`. Historical runtime ids whose effort cannot be
matched to current context stay at effort `unknown` and are counted.

The estimate is independent of the actual-token meter. The earliest managed
session with a selection receipt supplies `estimate.basis: receipt` and its
selected cell/cost. With no receipt, the highest-output observed tuple is
looked up in the accepted capability catalog (`catalog_lookup`); with no
matching cell or no observed tuple the estimate basis is `none`. `as_of` uses
the oldest available last-poll/event bound across the joined ids, and
`lag_seconds` measures its age at read time.

`read_session_usage_window` passes `since`, `until`, and `vendor` unchanged to
S1 exactly once. `priced` defaults to false; absent/false returns the exact S1
dictionary. With `priced=true`, it preserves every S1 field and adds
`pricing_by_model` (`usd` and `basis`), `unpriced_token_count`, and sorted
`unpriced_models`. It prices windowed totals, not cumulative Unit totals, and
does not accept a session-id argument. Reasoning tokens are a breakdown of
output tokens, and cache TTL/speed breakdowns are unpriced dimensions; the
count does not add either breakdown twice. If a category count is unavailable,
the answer retains the known count and names the lower bound in advisories.
Unknown model buckets remain present, count their known tokens as unpriced,
and never receive an invented price. Partial or bounded S1 status and
advisories remain intact.

The current per-runtime and per-source meter contract is:

| Runtime/source | Meter status | Detail |
| --- | --- | --- |
| `claude_code` / `claude_code_local` | Metered | Usage by message id. |
| `codex` / `codex_local` | Metered after Change 2 | Cumulative `codex_token_count` totals. Codex plan limits remain an assumption until spk-11 measures them. |
| `claude_code_cloud`, `claude_code_pushed` | Not metered | Raw identity only; usage is not persisted. |
| `claude_code_history` | Not metered | No per-message usage. |
| Other `codex_*` sources | Not metered | They do not provide the local Codex meter. |
| `claude_code_tasks`, `claude_ai_export`, `chatgpt_export`, `agent_messaging` | Not metered | No usage JSON or adapter. |
| Platform inference and `openai_embeddings_plugin` | Out of scope | These do not write session-ledger sessions; they need a separate source. |

Duplicate rows for one metered vendor session resolve to one S1 total and are
counted as `notes.sibling_rows_resolved`; they are not another meter. The
offline smoke registers a temporary `fixture_harness` adapter and a
`fixture_provider` metered profile in a temporary catalog. That fixture
proves the join and profile lookup can price a supported runtime without
adding fixture provider data to shipped source or the production catalog.

G1's word “actual” means tokens reported by a supported adapter. USD is actual
only when the provider supplies a metered amount; this interface computes a
catalog-based USD estimate and labels its basis. It does not turn a plan
allowance into token usage or a cash charge.

## References

- `plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/model_dispatch_policy.py`
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/model_capability_verbs.py`
- `plugin::agent_messaging_plugin::spawn_session`
- `plugin::agent_messaging_plugin::dispatch_managed_work`
- `plugin::agent_messaging_plugin::select_dispatch_tier`
- `quality_gates/schema_init_gate.py`

## Constraints

`select_dispatch_tier` accepts `effort_ceiling` (null or a known effort),
`exclude_model_prefixes` and `exclude_runtimes` (lists of non-empty strings).
The ceiling is inclusive. After separating unknown efforts, constraints filter
before feasibility, domination and the selected model's ladder. A dropped cell
has one reason: runtime first, model prefix second, effort ceiling last. Active
constraints add `constraint_runtime`, `constraint_model_prefix` and
`constraint_effort_ceiling` counts; unconstrained answers retain existing counts.
An empty constrained set returns `no_cell_clears_threshold` with those counts.
Clear or correct the inputs and select again; stale readings
remain advisory.

Every new immutable receipt includes the three inputs, defaulting to null and
empty lists. Spawn replay uses the same constraints and compares canonical copies;
predeploy seven-key receipts use absent defaults and return unchanged. Unknown
receipt fields still refuse. Correct or clear inputs by issuing a new selection,
never editing a saved receipt. Existing dispatch history provides readback; no
new mutable object or storage is introduced. Pin selection belongs to s2.


## Pin and receipt replay

`select_dispatch_tier` accepts `pin={runtime,model,effort}` and nonempty
`pin_basis` caller provenance under the existing process authorization. The
pin selects exactly one enabled accepted scored cell before per-call exclusions. Explicit runtime must match. Missing, disabled, withdrawn, unaccepted
or unscored cells return `pin_cell_unavailable` with the requested tuple and
nearest accepted scored cells in that runtime, ordered by score distance,
objective cost, model and effort. Catalog refresh/manual cell recording supplies
accepted scored measurements; pricing alone supplies no capability score.

A pin overrides threshold, effort/model/runtime constraints and capability floor; it never overrides operator enablement. Answers carry `selection_mode=pinned`, `pin_basis`,
`pin_overrides_constraints`, `pin_below_threshold`, and `pin_cost_delta` against
the cheapest feasible cell under the same objective. Unpriced pins and empty
feasible sets have null delta. Floor overrides carry `pin_floor_disallowed` warnings. Stale selected pins appear in readings and
advice even outside the feasible set. An unavailable advisory refresh-history
read is logged and warned about and retains the refresh request; normal history
reads still deduplicate. Catalog and receipt integrity faults still propagate.

New receipts contain the original seven keys plus `pin`, `pin_basis`,
`effort_ceiling` (null defaults), `exclude_model_prefixes` and `exclude_runtimes`
(empty-list defaults). Spawn replay canonicalizes absent additions in copies,
requires original keys, refuses unknown keys and selected-tuple mismatches, and
returns the supplied receipt unchanged. Predeploy seven-key receipts and saved
tiers need no replacement selection. Replay establishes selection consistency,
not historical provenance authenticity; managed-session retry integrity still
protects the stored receipt. Correct pin/basis by issuing a new selection; clear
both to null for cheapest mode. Issued receipts remain immutable. No new stored
object or lifecycle service is introduced.
