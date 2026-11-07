# Retired-Role Registry Interface

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:role_binding

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:agent-to-agent-coordination, evidence-category:operator-ruling, domain:agent-messaging, domain:role-binding

Embedding Description: The versioned read of retired fleet role names that project-solet's document and brief guard binds to: exact call, response shape, version semantics, normalization and the fail-closed contract.

## What it is

The solet's retired-role registry is the one authoritative set of role names
that are no longer in use (issue `iss_d9c955a2`, operator ruling
`arm-2305256f`, transport part (a)). It is one append-only table,
`agent_messaging_plugin__retired_role`. There is no other list: no static
fallback in any repository, and a vacant role binding is not evidence that a
role is retired. `Register-Lead` is always present; a migration seed adds it
at bridge start.

Inside this solet the registry is enforced at the transports. A send to a
retired name through `peer_send_by_name`, the bridge `send_by_name` route, the
streamable `peer_send_by_name` tool or `deliver_job_completion` is refused with
code `peer_role_retired` before anything is persisted. `peer_claim_role`
refuses to claim a retired name with the same code. Unknown and vacant names
behave as before (`peer_role_vacant`).

## Availability: nothing exists until the solet restarts on this code

The table, the `Register-Lead` seed and both verbs arrive with this code and
take effect only when the solet's bridge restarts on it. Before that restart,
on any solet:

- `list_retired_roles` is an unknown process, so the call fails;
- the table does not exist, or has no rows;
- transports do not refuse retired names.

A consumer must treat every one of those states as **registry unavailable**
and fail closed. None of them means "no role is retired".

## The call

```bash
solet-bridge call plugin::agent_messaging_plugin::list_retired_roles '{}'
```

A successful result's `data` is:

```json
{
  "registry_version": 1,
  "roles": [
    {
      "role": "Register-Lead",
      "retired_at": "2026-09-28T00:00:00+00:00",
      "retired_by": "iss_bfdb51c1-7a31-443f-b9c9-e15a6bcd7d8c",
      "reason": "role retired; triage is filed as project-solet register tickets",
      "successor": null,
      "registry_version": 1
    }
  ]
}
```

`roles` is ordered by the `registry_version` each entry was added at. `role` is
the display spelling the name was retired under.

## Version semantics

- `registry_version` is an integer of at least 1. It is the highest version of
  any entry.
- Every retirement appends one entry at the registry's version plus one. Entries
  are never edited or deleted, so the version strictly increases and never
  repeats. The column is unique, so two concurrent retirements cannot share a
  version.
- A consumer may cache the set keyed by `registry_version` and refetch when a
  later read reports a different version. Two reads with equal versions hold
  the same names.
- Un-retiring is not supported. If it is added later, it must append a
  tombstone entry at a new version, so this contract still holds.

## Name matching

Inside the solet, names match after this normalization, in this order:
Unicode NFKC, then `str.casefold()`, then stripping surrounding whitespace.
`register-lead` and a full-width spelling therefore match `Register-Lead`. A
consumer that matches names in documents should apply the same normalization to
both sides and declare any further rules (for example word boundaries) itself.

## Failure contract

- Every read failure is an error result, never an empty list: a failed page,
  a malformed entry, and an empty registry (the seed has not run) all return
  `action_status: failed` with code `retired_role_registry_unavailable`.
- A consumer must fail closed on any error, an unknown-process error (the
  solet has not restarted on this code yet), a non-zero exit from
  `solet-bridge`, a missing `registry_version`, or an empty `roles`: refuse the
  registration or brief it was checking. It must not substitute a hard-coded
  name such as `Register-Lead`.

## Retiring a role

`plugin::agent_messaging_plugin::retire_role` takes `name`, `reason`,
`authority` (the ruling or issue that authorizes it) and an optional
`successor`. Only a caller holding a primary- or principal-class role may call
it. It refuses a role that a live session holds until that holder runs
`peer_release_role`. The result is the new entry, including the registry's new
`registry_version`.
