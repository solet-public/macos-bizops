# Model Dispatch Policy

Tags: knowledge:tag:plugin_reference, knowledge:tag:dispatch_policy, knowledge:tag:model_assignment

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:agent-to-agent-coordination, evidence-category:operator-ruling, domain:agent-messaging, domain:managed-dispatch

Embedding Description: How the platform enforces the required Claude-orchestrator and Codex-Terra implementation dispatch policy at spawn time, during paired diagnosis/design sweeps, and at the main seat hook.

## Ruling

The accepted operator ruling is: “So from now on, we will be using Sonnet five
as our orchestrator. We will be assigning all work to Terra.” The enforcement
point is `plugin::agent_messaging_plugin::spawn_session`, not a plan or memory
note, because every managed dispatch passes that verb.

The declarative source is
`plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`. It is loaded
for every spawn and validated against model identities declared under
`plugins/agent_messaging_plugin/model_profiles/`; a missing, malformed, or
unknown policy model refuses rather than choosing a fallback.

| Dispatch kind | Required assignment |
| --- | --- |
| `diagnose` / `design` | Two producers: codex `gpt-5.6-sol` and claude_code `claude-opus-5`, sharing a `pair_id` |
| `review` | codex `gpt-5.6-terra` or claude_code `claude-sonnet-5`, but the reviewer must be the other vendor than `reviewed_report_vendor` |
| `fix` | codex `gpt-5.6-terra` |
| `infrastructure` | Any runtime/model pair; it remains recorded on the ledger |

The `Main` orchestrator-role suffix may use only `claude-sonnet-5` or
`claude-fable-5`. The tracked seat hook reads the latest usage-bearing
assistant model from the same transcript source as the tracked rotation-watch
hook. An invalid seat model produces an
`ORCHESTRATOR_MODEL_VIOLATION` line and blocks only
`spawn_session` / `dispatch_managed_work` tool calls until the seat relaunches
with the stated `--model` flag.

## Spawn arguments and refusals

`dispatch_kind` is mandatory for `plugin::agent_messaging_plugin::spawn_session`
and `plugin::agent_messaging_plugin::dispatch_managed_work`. `pair_id` is
mandatory for `diagnose` and `design`; `reviewed_report_vendor` is mandatory
for `review` and must be `codex` or `claude_code`.

- `dispatch_kind_required`: no kind was supplied.
- `dispatch_policy_violation`: the pair is not allowed, a review is same-vendor,
  or a required pair/review field is absent.
- `dispatch_policy_unavailable`: the policy file cannot be read.
- `dispatch_policy_invalid`: the policy shape or its model-profile references
  are invalid.

`managed_session` records `dispatch_kind`, `reviewed_report_vendor`, and
`pair_id`. `session_sweep.py` sends a `dispatch_policy_unpaired` notice to the
spawning role when a live diagnose/design producer has no live other-vendor
partner with the same `pair_id` within ten minutes of spawn. The notice is a
delivery reminder, not a synthetic second producer.

## Scope boundary

This policy does not create composite dispatch verbs. A later
`dispatch_diagnosis` or `dispatch_review` workflow can consume the three
ledger fields, policy JSON, and unpaired notice, but it must still explicitly
create and drive both producer/reviewer sessions.

## References

- `plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/model_dispatch_policy.py`
- `plugin::agent_messaging_plugin::spawn_session`
- `plugin::agent_messaging_plugin::dispatch_managed_work`
