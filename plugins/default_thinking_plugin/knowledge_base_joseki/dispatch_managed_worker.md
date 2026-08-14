# Dispatch Managed Worker

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:agent-lifecycle, domain:agent-messaging


JOSEKI_KEY: dispatch_managed_worker
DESCRIPTION: Spawn a worker session onto a bounded lane and stay responsible for it: write the brief to the workbench FIRST so the spawn carries a citable provenance reference, spawn with an explicit role class, work class, budget line, model, effort and TTL plus a report-by deadline generous enough to survive a lost registration, confirm the worker is actually reachable, drive its first work turn directly when it is not, require a per-item plan read-back before it edits anything, hold it off files another lane is editing, and accept only a workbench report whose file list was derived from git status. Use when work is parallelizable, mechanical, or long-running enough to belong in its own session. Not for work the dispatching session can finish inline, and never a path to escalate a capability the dispatcher does not already hold.
EMBEDDING_DESCRIPTION: Start another agent session to do a piece of work and supervise it until it delivers. Write the instructions to a shared document first so the spawn can point at them, then create the session with its purpose, permitted work class, budget line, model, effort level, time limit, and a deadline for its first report chosen long enough that a lost registration does not get a working session killed. Check that the new session is reachable, and if it is not, send its first work turn straight to its terminal instead. Make it repeat back its plan before it changes any file, keep it off files another lane is editing until a go arrives on its own channel, require progress reports before its deadline, and accept only a final written report listing the files it actually touched.

## Input Contract

- A bounded, self-contained unit of work that is worth its own session: parallelizable, mechanical, or long-running
- A lane identity for the work, and a declared file surface the worker may edit
- A budget line the spawn rolls up to, and a deliberate model / effort / TTL choice matched to the work rather than inherited from the dispatching session
- Bindings: lane_id, brief_ref, role_class, work_class, budget_line, model, effort, ttl_seconds, report_by_seconds, worker_instance_id, dispatch_text

## Output Contract

- A workbench brief that exists BEFORE the spawn and is cited by it, so the dispatch has provenance independent of any session's memory
- A spawned worker session with its identity, host, and lifecycle state recorded, whose dispatch parameters were all stated explicitly at spawn time
- Evidence that the worker received its work — either a confirmed registration and a plan read-back, or a verified direct drive of its first turn
- A final workbench report from the worker whose file list was derived from git status over its declared surface, plus the per-item progress reports that arrived inside the report-by window

## Sequence

[ ] 1. Write the brief to the workbench before spawning anything
    a) Author the lane brief — scope, owned file surface, constraints, per-item deliverables, and the reporting cadence expected — as a workbench artifact whose path becomes the spawn's provenance reference [agent-executed: the brief is authored, not produced by a verb]

[ ] 2. Spawn the worker with every dispatch parameter stated explicitly
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Spawn the worker session for this lane (plugin::agent_messaging_plugin::spawn_session)
        Arguments:
        {"lane_id": "<<BIND:lane_id>>", "brief_ref": "<<BIND:brief_ref>>", "role_class": "<<BIND:role_class>>", "work_class": "<<BIND:work_class>>", "budget_line": "<<BIND:budget_line>>", "model": "<<BIND:model>>", "effort": "<<BIND:effort>>", "ttl_seconds": "<<BIND:ttl_seconds>>", "report_by_seconds": "<<BIND:report_by_seconds>>"}

[ ] 3. Confirm the worker is actually reachable
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Read the live peer snapshot and look for the spawned instance (plugin::agent_messaging_plugin::peer_list)
        Arguments:
        {}

[ ] 4. Drive the first work turn directly when the worker is not reachable
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Dispatch the work turn into the spawned session over its host driver (plugin::agent_messaging_plugin::drive_session)
        Arguments:
        {"agent_instance_id": "<<BIND:worker_instance_id>>", "text": "<<BIND:dispatch_text>>"}

[ ] 5. Require a per-item plan read-back before the worker edits anything
    a) Require and read the worker's per-item plan read-back, and correct any item whose scope drifts from the brief before it touches a file [agent-executed: the read-back is the dispatcher's last cheap correction point]

[ ] 6. Hold the worker off any file another lane is editing until a named go
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Send the hold, and later the explicit go, for each shared file on the worker's own channel (plugin::agent_messaging_plugin::peer_send_by_name)
        Arguments:
        {"name": "<<BIND:worker_role_name>>", "content": "<<BIND:hold_or_go_message>>"}

[ ] 7. Accept the deliverable only as a workbench report with a derived file list
    a) Accept the worker's final workbench report, confirm its file list was derived from git status over the declared surface rather than written from memory, and reconcile it against the brief's item list [agent-executed: acceptance is the dispatcher's judgment, not a verb]

## Expected Step Count

7 steps for a full supervised dispatch; step 4 is skipped when step 3 shows the worker reachable and its first turn already delivered, and step 6 is skipped when the lane shares no file with another lane.

## Binding Guidance

- Bind `brief_ref` to the artifact written in step 1, and write that artifact BEFORE step 2 runs. The spawn records the reference as provenance; a reference to a file that does not yet exist records a promise instead of a brief.
- Bind `role_class` and `work_class` deliberately: the role class decides lifecycle expectations, the work class decides what the worker is permitted to do. Choose the narrowest work class that lets the lane finish.
- Bind `model`, `effort`, and `ttl_seconds` explicitly on EVERY spawn, matched to the work — a mechanical or gate-running lane on a cheaper tier with bounded effort, a judgment-heavy lane on a stronger one — never inherited silently from the dispatching session. Bind `budget_line` to the ledger key the spawn rolls up to, so the cost lands somewhere accountable.
- Bind `report_by_seconds` generously. Liveness in the session ledger keys on the worker's peer registration, so a worker whose registration is lost to transport churn can be reaped while it is alive and working. Until that is fixed at the platform, a report-by window long enough to outlast a churn window is what keeps a healthy worker from being killed for a transport fault, and a direct pane drive is the expected fallback rather than an exception.
- Bind `worker_instance_id` in step 4 to the identity step 2 returned, re-confirmed against step 3's snapshot rather than remembered — instance identity changes across a reconnect, and a remembered one silently addresses a session that no longer exists.
- Bind `dispatch_text` to a SELF-CONTAINED first turn: the brief's path plus the instruction to read it in full and reply with a plan read-back. A driven turn that assumes context the worker never had produces a confident worker doing the wrong lane.
- Bind step 6's `worker_role_name` to the worker's OWN channel identity. A go relayed through a third party, or observed on someone else's surface, is not a go for this worker.
- Where a lane's founding words were captured as a charter before the spawn, the spawn resolves the latest charter for that lane and drives it as the worker's literal first turn; step 4's drive is then a fallback for the case where no first turn was delivered, not a second dispatch. Sending a second first-turn to a worker that already got one produces two competing readings of the same lane.

## Coherence Obligations

- The brief exists before the worker does. A spawn whose brief is written afterwards has no provenance at the moment it matters — the worker's first turn — and every later reconstruction of "what this lane was told" is a memory claim.
- A session that appears in a presence listing is not a session that received its work. Presence is evidence of a process; a plan read-back is evidence of a briefed worker. Treat a missing read-back as a delivery failure and drive the turn, not as a worker that is thinking.
- When the driver path is unavailable and the worker's terminal must be driven by hand, send the text and the newline as SEPARATE actions and then read the pane back to verify the turn was accepted. A paste and its newline in one burst can be swallowed, leaving a fully composed dispatch sitting unsent in the input line, which reads exactly like a worker that received the work and went quiet.
- Never let a worker's edits ride into another lane's staging window. Files shared between lanes are held until an explicit go arrives on the worker's own channel, and the dispatcher owns sequencing the two lanes; an operator-surface relay of a go is not a go, and neither is a summary the dispatcher wrote itself.
- The dispatcher stays responsible after the spawn. A worker that misses its report-by window is chased or stood down deliberately; silence is not a status, and a lane left running past its usefulness spends budget on nothing.
- This card dispatches work the dispatcher is already permitted to do. It never mints a capability the dispatching session does not hold, and a lane that turns out to need one routes back to the surface that can grant it rather than working around the gap.

## Next Joseki

`request_scoped_landing` — the usual successor when the dispatched lane produced edits that must reach the main branch. The worker writes its own evidence record and file list; the landing request is a separate, verified exchange with Git-Controller and does not happen implicitly when a lane reports done.

## Repair Joseki

Explicitly absent as a card. The failures are distinguishable and take different repairs: a spawn that returns no session is read for its own error before any retry, since a blind second spawn can leave two sessions on one lane; a session that spawned but never registered is driven directly rather than re-spawned; a worker that registered but never read back its plan is re-driven with the same self-contained text, never with a shortened one; a worker reaped while alive is re-spawned with a longer report-by window and the same brief reference. A worker that has already edited files is never abandoned silently — recover its file list from git status over its declared surface before standing it down, or the edits become an unowned dirty tree for the next lane to inherit.
