# Usage Meters and the Extension Contract

Tags: knowledge:tag:plugin_reference, knowledge:tag:usage_meters, knowledge:tag:agent_messaging

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:operator-runbook, planning-stage:provider-extension, evidence-category:invariant-contract, domain:agent-messaging, domain:usage-accounting

Embedding Description: How operators interpret allowance readings, measured token usage, and priced windows without converting between them, including source coverage, unknown-model handling, lifecycle readback, and the bounded extension path for a new usage adapter or provider profile.

## Purpose and the two meters

Use this article when a close-out, desk, or operator decision needs to explain
what a usage reading means. An allowance-pool reading is the provider or
operator's percentage/remaining-capacity signal. A usage reading is measured
tokens from a supported harness adapter. They are different meters: never
derive an allowance percentage from tokens, and never convert tokens into USD.
The dated profile `anthropic-max-20x-2026-08-22` is an example of an accepted
profile identifier; it is not an undated catalog name. The priced result is a catalog-based estimate unless the provider supplied a
metered amount, and always carries its pricing basis.

`read_unit_usage` is the per-Unit snapshot: it joins managed sessions, reads
bounded S1 totals, and reports coverage, advisories, observed model/effort,
pricing, and the latest allowance reading. It is cumulative for the Unit as of
the read's bounds. `read_session_usage_window(since, until, vendor, priced)`
answers an event-time window. Raw mode is exactly S1; priced mode adds
`pricing_by_model`, `unpriced_token_count`, and `unpriced_models`. The wrapper
passes the window and vendor unchanged once per signal and does not fan out by
managed session.

## Meaning of a reading

`UsageVector` preserves null or unavailable categories. Actual means tokens
reported by the adapter; USD is nullable and is accompanied by `pricing_basis`.
Unknown model and effort remain unknown. In particular, an absent
`usage_json.model` is literal unknown, never a guess from dispatch, session,
neighbor, or current context. Unknown models are counted as unpriced rather
than assigned an invented rate. Reasoning is an output-token breakdown, not an
additional total; cache TTL/speed dimensions are unpriced where unsupported.

Coverage is `full` only when every managed worker has a runtime id and each id
has complete supported usage or a measured supported zero. Missing mappings,
unsupported sources, partial reads, unresolved ids, and bounded reads remain in
advisories/notes. With no usable supported reading or confirmed zero, coverage
is `none`; missing runtime data is advisory and does not block a read.

The current source fence is explicit:

| Runtime/source | Status | Contract |
| --- | --- | --- |
| `claude_code_local`, `codex_local` | Metered when ingestion exists | Message-id dedupe for Claude; cumulative Codex totals after Change 2. |
| `claude_code_cloud`, `claude_code_pushed`, `claude_code_history` | Not metered | Raw identity or history only; no persisted per-message usage. |
| Other `codex_*`, Codex cloud/pushed/history/ambient/state/goals/memories | Not separate meters | Only usage-bearing eligible sources contribute. |
| `claude_code_tasks`, `claude_ai_export`, `chatgpt_export`, `agent_messaging` | Not metered | No adapter or usage JSON. |
| Platform inference and embeddings | Out of scope | They require a separate source. |

Duplicate `(vendor, external_session_id)` rows resolve to one usage-bearing
winner. The result reports `sibling_rows_resolved` and source limitations; it
is not a union-of-all-rows promise. Codex plan limits remain an assumption
until the planned spike measures them.

## Pool and usage lifecycle

Allowance readings use the existing last-write-wins record lifecycle: record a
reading, read current or historical values, correct/re-record it, retract or
clear it when the source says it is no longer valid, and read the cursor/expiry
metadata. Re-recording is not a new CAS protocol. Expired or missing readings
remain visible and advisory. A Unit snapshot similarly has current and
historical readback; it is not a claim that tokens were consumed after the
`since` time. Window answers are event-time results, while desk snapshots are
cumulative whole-Unit results whose newest authoritative snapshot is selected
by the requested bound.

## Extending a provider or harness

An extension adds a real adapter/provider conformance fixture and the matching
native registration/evidence; it does not add a schema or a core-to-plugin
import. The fixture harness may live in the temporary smoke catalog, but
fixture provider data does not ship in the production catalog. Document the
source identity, dedupe key, supported categories, null behavior, pricing
basis, and known gaps. A new model is not priced merely because it appears in a
catalog: measure a provider rate first. The landed fixture/provider evidence
proves the join and profile lookup path; it is not a live reindex or deployment
claim.

## Selection, receipts, and review boundaries

Dispatch pins and constraints remain the KB14 contract. Absent added receipt
keys replay as null/empty defaults, preserving saved tiers; unknown receipt
keys still refuse. A pin can override threshold and constraints but not
operator enablement. For a separate-session review, record the exact candidate
identity and cited source readbacks, then capture a typed outcome separately
from physical evidence. An inert or unknown flow-policy reading is advisory;
the operator inspects wired status before treating it as active. Landing/watch
and later-stage signal ladders have their own limitations and are not implied
by these usage meters.

## Authority and close-out

Revision 2 of the factory plan governs this documentation contract. The
canonical Change Close-Out Checklist owns issue disposition, documentation
impact, reviewer follow-ups, and lane retirement. Source checks and the native
shipped-document gate establish document integrity only. After landing, the
seat must reindex the current Markdown through its canonical path and search
for this title and “usage meters extension contract,” recording retrieval ids
and results. Until that happens, indexed-knowledge acceptance remains pending;
this article does not claim a live reindex, deployment, or retrieval pass.

## References

- [Model Dispatch Policy](14_model_dispatch_policy.md), especially “Usage and the two meters,” “Constraints,” and “Pin and receipt replay”
- `plugin::agent_messaging_plugin::read_unit_usage`
- `plugin::agent_messaging_plugin::read_session_usage_window`
- Project Solet flow-policy create, flow release, and policy revoke verbs
- canonical Change Close-Out Checklist (issue disposition, documentation impact, reviewer follow-ups, and lane retirement)
