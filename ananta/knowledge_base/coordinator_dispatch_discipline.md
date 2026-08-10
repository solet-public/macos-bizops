Tags: coordinator, dispatch, watchdog, multi-agent, peer_send, scheduling, in_flight, reliability, discipline
Article Layer: 2

# Coordinator Dispatch Discipline — Watchdog Pattern for In-Flight Tracking

When a coordinator session dispatches work to a peer session (or sub-agent, or backend thread), there is an **unmonitored gap** between "I sent the dispatch" and "the peer reports completion." In that gap the dispatch can stall — the peer can hit a blocker and not surface it, an MCP bridge can drop, a sub-agent can finish in a worktree that gets discarded — and the coordinator only notices when it opportunistically pings or when the operator asks "where are we?"

This article documents the discipline that closes that gap: **every dispatch that expects a deliverable declares the deliverable in the brief AND schedules a watchdog**. The watchdog runs on a platform-driven timer; when it wakes, it checks for the deliverable and pings the peer if neither the deliverable nor a completion signal has arrived.

The underlying platform mechanism (memory-driven scheduling + the wake-up Vertex pattern) already exists. The article that documents that mechanism is `responsiveness_and_checkins.md`; the underlying scheduler primitive is documented in `scheduling_memory_driven.md`. This article applies that mechanism to the specific case of **coordinator-to-peer dispatches**.

---

## 1. The failure mode this addresses

Failure F1 in the v2 coordinator workflow strategy: **the coordinator dispatches and then loses track until it manually remembers to check**.

The empirical pattern:

- Coordinator sends a `peer_send` dispatch to a peer with a work brief.
- The peer wakes, starts working.
- Something happens — peer is mid-task, takes longer than expected, hits a blocker but waits to ask, the dispatched session uses a worktree that vanishes, MCP bridge drops.
- The coordinator has no scheduled prompt to re-check. The operator notices the gap before the coordinator does.

The dispatch chain is structurally **fire-and-forget** unless the coordinator actively builds a follow-up loop into it.

---

## 2. The dispatch envelope — four declared ingredients

**Every coordinator dispatch that expects an output file or a completion signal** declares four ingredients in its brief, explicitly named:

```
expected_path: <repo-relative path that the peer will produce or modify>
expected_completion_signal: <the form the completion report will take>
kb_search_required: <list of plain-English queries the peer MUST run via
                    service_interface::knowledge_service::search before
                    making any design or implementation decisions>
ledger_search_required: <list of plain-English queries the peer MUST run via
                    service_interface::session_ledger_service::search_event_content
                    (and search_sessions when summary granularity suffices) to
                    check whether the task already has prior art in the
                    conversation record before building>
```

`expected_path` is mechanical — the coordinator can check `os.path.exists()` (or `git status`) at watchdog wake-up time and know whether the work landed without parsing any messages.

`expected_completion_signal` is the peer-side commitment. For peer-to-peer dispatches it is almost always: `peer_send to Coordinator (agi-<id>) with verdict + before/after counts`. For dispatches that produce a file, the file path serves the same role (existence-check at wake-up).

`kb_search_required` is the **non-negotiable Step Zero gate in the paired
AGENTS.md / CLAUDE.md bootstrap contract**. The dispatch enumerates the searches
the peer must run before opening the editor — typically 3–6 queries spanning
(a) the platform mechanism the work touches, (b) any canonical pattern the
work might be reinventing, and (c) the contract surface the work integrates
with. The peer's completion report should cite which articles surfaced and how
they shaped the implementation. **Coordinator dispatches that omit this field
have produced demonstrable rework** — source-pointer-only briefs let peers skip
Step Zero and reinvent a documented convention, which then surfaces as a stack
of corrections during review. The field exists to make the discipline
structural rather than aspirational.

`ledger_search_required` is the same pre-flight logic pointed at the conversation record rather than the knowledge base. `kb_search_required` catches a peer reinventing a documented convention; `ledger_search_required` catches a peer rebuilding something an earlier session already built, where the only trace is the verbatim message text now searchable via `service_interface::session_ledger_service::search_event_content` (LED-01). The dispatch enumerates the plain-English queries the peer must run through the event-content search — and through `search_sessions` where summary granularity suffices — before opening the editor, and the completion report cites what the recall surfaced and how it shaped the work, exactly as `kb_search_required` already asks. The full four-surface recall discipline this field enforces is specified in `knowledge_bases/ananta_platform/14_knowledge_retrieval/04_recall_before_work.md`.

All four fields go in the dispatch prose. expected_path and expected_completion_signal are durable references the watchdog can act on; kb_search_required and ledger_search_required are the peer's pre-flight recall contract against the knowledge base and the conversation record respectively.

---

## 3. The watchdog — `scheduling_service::execute_in_seconds`

Immediately after sending the dispatch, the coordinator schedules a watchdog via the platform's memory-driven scheduling primitive:

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 1800,
    "memory_tag": "dispatch:watchdog:<task_id>",
    "content": "Watchdog for dispatch <task_id>: peer=<peer_id>, expected_path=<path>, expected_completion_signal=<signal>. On wake: (a) check path exists; (b) check peer_inbox() for completion since <T>; (c) if neither, send a status-check peer_send and reschedule."
  }
}
```

The one-step pattern stashes the wake-up instruction as a tagged memory automatically. No separate `memory_service::remember` step is needed for the watchdog content; the `content` argument to `execute_in_seconds` IS the stash.

The `memory_tag` is the unique identifier of this watchdog. Convention: `dispatch:watchdog:<task_id>` where `<task_id>` is the TaskCreate task number. This lets later cleanup (`clear_scheduled_actions_by_tag`) target the watchdog precisely if the dispatch resolves before the watchdog fires.

---

## 4. The wake-up routine

**Important limitation:** the wake-driven path described below depends on the coordinator session being in a VERTEX turn at fire time. EDGE-scheduled-action wakes don't surface as channel notifications to the originator session. In practice this means the mechanical state-check (steps 1–2 below) IS the load-bearing mechanism; the wake-driven trigger (step 3 "send a status-check ping") only fires when the coordinator happens to be in a VERTEX turn (typically because the operator just interacted). The discipline degrades gracefully: the coordinator runs the state-check at the start of any turn with in-flight work, wake-driven or operator-driven.

When the schedule fires AND the coordinator is in a VERTEX turn, the coordinator wakes with the watchdog memory presented as an observation. The mechanical check is:

1. **`expected_path` exists on disk?** → if yes: dispatch completed. Mark the in-flight memory completed (`memory_service::remember` with a `dispatch:completed:<task_id>` tag, or delete the in-flight entry). STOP.
2. **`peer_inbox()` contains a completion `peer_send` from the dispatched peer since dispatch time?** → if yes: same as (1).
3. **Neither?** → Send a `peer_send` to the dispatched peer:

   ```
   Status check on dispatch <id> sent at <T>. Expected path: <path>.
   Expected signal: <signal>. Neither has landed. Reply
   with status (done/in-progress/blocked) + ETA or blocker.
   ```

   Then re-schedule the watchdog for another 1800s via `execute_in_seconds`.

The coordinator does NOT cancel the dispatch on the first wake — peers can legitimately take longer than 1800s. The cycle is **status check → reschedule** until the deliverable arrives or the peer reports a blocker.

**Per-turn state-check is the primary path:** because the wake-driven trigger requires a VERTEX turn at fire time (see §4 limitation above), the coordinator's standing discipline is to run the steps 1–2 mechanical check at the start of any turn with in-flight work. The watchdog scheduling remains useful as a tagged memory marker — it lets the coordinator find in-flight dispatches via `memory_service::get_memories_by_tag` even if the wake hasn't fired into a session — but the wake firing isn't the prerequisite for catching completion. The deliverable-on-disk check is.

---

## 5. Why 1800 seconds

1800s (30 minutes) is the default cadence based on empirical observation: clean-path peer dispatches in the platform's history have completed within ~600s (10 min); architect one-shots have taken 25 min; god-class decomposition analyses have run 15–40 min. 1800s ≈ 3× the median — conservative enough to avoid false positives, aggressive enough to catch a stuck dispatch within one operator-attention-window.

**This is wall-clock time used as a platform-driven scheduling primitive, NOT a human-effort projection.** The no-wall-clock-estimates rule applies to estimating how long agent work will take; the watchdog cadence is a scheduling parameter.

For dispatches with known-longer expected duration (e.g., a Codex multi-hour pass), the coordinator should override with a longer initial delay (`seconds: 7200` for 2-hour work) and document the override in the watchdog content.

---

## 6. Alternative trigger — K=3 coordinator-side sweep

The 1800s timer is one trigger. An additional trigger: **on every 4th dispatch the coordinator sends, sweep `memory_service::get_memories_by_tag(tag="dispatch:in_flight")` and emit status pings for any entry older than the median dispatch duration**.

This is event-driven where the timer is time-driven. The two combine: whichever fires first.

K=3 is NOT a platform feature. The scheduler does not support event-count triggers today. K=3 stays a **coordinator discipline** — the coordinator counts its own dispatches and runs the sweep manually. Extending `scheduling_service` with event-count triggers is a possible future enhancement, not a current requirement.

---

## 7. Session-level cadence — the 20-minute active-management cron

The §3 per-dispatch watchdog catches a specific stuck dispatch. The §6 K=3 sweep is event-driven and only fires when the coordinator is actively dispatching. Neither closes the failure mode where **a peer ACK'd, started work, and then went silent across a long quiet stretch while the operator was heads-down on something else**. The coordinator's own session has no in-flight inference until the operator next interacts, and the per-dispatch watchdog only fires once per task at T+1800s.

The **20-minute active-management cron** closes that gap. While the coordinator is actively managing in-flight work, a recurring cron fires every 20 minutes. Each fire is a forced sweep regardless of any particular dispatch state.

`create_cron_schedule` does NOT accept a `content` parameter (unlike `execute_in_seconds`, which does). Use the **two-step pattern**: upsert the wake-up content under the same `memory_tag`, then create the cron. The scheduler's wake-up runs `get_memories_by_tag` and surfaces the upserted memory as the observation.

```json
// Step 1 — upsert the wake-up content (single canonical memory for this tag)
{
  "process_key": "service_interface::memory_service::upsert_memory_by_tag",
  "arguments": {
    "tag": "coordinator:active_sweep",
    "content": "Active-management sweep: (1) read mcp__<server-name>__peer_inbox() for milestones, completions, questions, blockers; (2) sweep memory_service::get_memories_by_tag(tag='dispatch:in_flight') for any dispatch past its expected_completion_signal window; (3) for any peer who ACK'd but hasn't surfaced a milestone or completion within 2× their stated ETA, send a status check; (4) update your coordinator plan doc if state changed; (5) if everything is quiet AND no in-flight dispatches remain, decide whether to clear this cron via clear_scheduled_actions_by_tag(tag='coordinator:active_sweep')."
  }
}

// Step 2 — create the recurring cron keyed by the same tag
{
  "process_key": "service_interface::scheduling_service::create_cron_schedule",
  "arguments": {
    "cron_expression": "*/20 * * * *",
    "memory_tag": "coordinator:active_sweep",
    "tags": ["coordinator:active_sweep"],
    "label": "Coordinator active-management sweep"
  }
}
```

Use `upsert_memory_by_tag` (not `remember`) so the wake-up slot is a single canonical memory — no accumulation across cycles.

Clear the cron when standing down or when work quiesces:

```json
{
  "process_key": "service_interface::scheduling_service::clear_scheduled_actions_by_tag",
  "arguments": { "tag": "coordinator:active_sweep" }
}
```

**Why 20 minutes.** Calibrated against the empirical pattern of a multi-peer campaign: dispatched peers typically reach a meaningful milestone within 10–30 minutes; operator attention windows are typically 30–90 minutes when heads-down on something else. 20 minutes is fast enough to catch a silent-stuck peer within one operator-attention-window, slow enough that the cron isn't burning inference on no-op sweeps. In a long unattended multi-peer campaign this cadence catches silent-stuck cases that would otherwise have waited until the operator's next check-in.

**Distinguished from §3 per-dispatch watchdog.** Per-dispatch watchdog fires ONCE at T+1800s for a specific `<task_id>`; the active-management cron fires EVERY 20 minutes regardless of any particular task state. The two compose: per-dispatch catches a specific stuck dispatch; cron catches the cross-dispatch case where a peer ACK'd but went silent mid-task, drifted past their stated milestone cadence, or where the in-flight memory state aged past expected window.

**Distinguished from §6 K=3 sweep.** K=3 is event-driven (every 4 dispatches); the cron is time-driven. The cron is the time-driven version of the K=3 discipline, with the critical advantage that **it fires even when the coordinator isn't actively dispatching** — which is precisely the failure window K=3 cannot cover.

**Handoff hygiene.** A coordinator who hands off to another coordinator session (operator-driven `/rename` or session restart) MUST clear the cron from their session before standing down. The new coordinator establishes their own cron under their own session. Leaving a cron firing in a standby session burns inference tokens at 20-minute intervals on a session that is no longer the active coordinator and produces no useful work. The handoff brief MUST explicitly call out either "cron deleted" or "cron live under tag X — adopt or replace."

---

## 8. The dispatch / ACK / milestone protocol

**A4 (2026-08-04): delivery is unconditional now.** Every `peer_send` is delivery-attempted against the resolved recipient's live binding, whether or not the prose starts with `IMPORTANT` — the platform mechanism is documented at `knowledge_bases/agent_messaging_plugin/03_inter_agent_messaging.md`. A leading `IMPORTANT:` is still recognized and stripped before delivery, but it is now purely cosmetic emphasis for the reader; it does not gate whether a wake fires. This section codifies the **coordinator-side discipline** for using `peer_send` well under that contract — what used to be enforced by a marker choice is now enforced entirely by whether you send a message at all.

Coordinator-to-peer dispatch has three asymmetric touchpoints on the same lifecycle:

### A. Coordinator → Peer: dispatch is a `peer_send`

Every coordinator-issued dispatch is sent as soon as the brief is ready — delivery is attempted unconditionally, so there is no separate step to "arm" the wake. The dispatch envelope (§2) is the minimum viable dispatch.

### B. Coordinator → Peer: dispatch DEMANDS an ACK before work starts

The dispatch brief includes an explicit demand: "ACK before starting work." Two purposes:

1. **Engagement confirmation.** No ACK within a reasonable window means the coordinator cannot distinguish "engaged and working" from "the wake didn't land." An ACK confirms the peer is at inference and surfaced the brief.
2. **Pre-work clarification.** ACK is the structural moment for the peer to surface scope questions, interpretation ambiguity, or blockers BEFORE editing. The cost of an ACK-and-question cycle is bounded; the cost of an in-flight scope misunderstanding caught at completion is unbounded.

Acceptable ACK content: "ACK. Will run KB searches per kb_search_required, then start. Expect a reply at completion." Or: "ACK. One scope question before I start: <Q>."

### C. Peer → Coordinator: REQUEST a reply at milestones / blockers / completion

The dispatch brief explicitly asks for a reply at meaningful milestones, blockers, and completion. Silence at completion is a structural error: the coordinator does not poll inbox between operator turns; silent completions are caught only via the §7 20-minute cron (delayed) or the next operator interaction (operator-driven coordination tax).

What counts as "meaningful" enough to report at all (see the two forms below for the mechanics):
- **Completion.** Always report.
- **Blocker.** Always report. The coordinator may be holding gating dispatches on the peer's verdict.
- **Milestone in a long-running task.** Report when downstream dispatches depend on the milestone landing.
- **Routine FYI status during a long stretch.** Sending nothing at all IS acceptable here — but the threshold is narrow.

### Two communication forms, not three

Before A4, a sender chose between an IMPORTANT-marked wake and a silent-persist that stayed in the thread without waking anyone — and silent-persist doubled as the ack-loop escape valve. That middle form no longer exists: **every `peer_send` now wakes the recipient unconditionally**, regardless of what the prose starts with. There are only two forms left, and conflating them is the mistake this section exists to prevent:

| Form | Mechanism | When to use |
|---|---|---|
| **`peer_send`** | Wakes the receiver via notification, unconditionally | **DEFAULT for all substantive content.** The receiver must act, decide, respond, or factor the content into their plan. Includes "concur, proceed" confirmations on a peer's stated default, FYI-with-substance, queued follow-on briefs, corrections, position flips. If it has substance, send it. |
| **No `peer_send` at all** | Nothing in the thread | The receiver's correct default IS "proceed without my input" and there is genuinely nothing to add — OR the content is a pure ack/thanks/"got it" with no further information for the receiver. E.g., peer announced completion and the next substantive action is a fresh dispatch to a DIFFERENT peer; internal coordinator state updates (TaskUpdate, plan-doc edits) that don't change any peer's plan; a peer explicitly said "proceeding unless you flag otherwise" and you have no flag; closing an ack-loop ("received, no further action needed") that would otherwise just wake the peer to acknowledge your acknowledgement. |

**Ack-loops are now avoided by not sending, not by choosing a marker.** A peer sends the coordinator a completion report. The coordinator wants to close the loop for audit-trail closure — but there is no low-wake-cost channel to do that in anymore: a reply, marked IMPORTANT or not, wakes the peer the same way. If the reply carries no substance the peer needs to act on, don't send it; the persisted thread already carries the completion report for audit. If you genuinely need to say something (a correction, a new instruction), send it — the wake is the cost of using the channel at all now.

**There is no low-cost channel for content that "feels low-stakes."** If you find yourself wanting to send something because "I don't want to interrupt the peer's focus" or "I want to queue a follow-on without scope-stacking," the old middle ground (silent-persist) is gone. Either the content is substantive (send it — with explicit "complete current task first" framing if it's a queued follow-on) or it isn't (send nothing).

**"Concur, proceed" confirmations specifically.** When a peer ACKs your dispatch with N pre-draft scope questions and stated defaults ("proceeding with these unless you flag otherwise"), your concur-with-defaults reply IS substantive — it removes their uncertainty about whether you've actually engaged with their plan. Send it. The wake is justified by the substance.

No `peer_send` at all is correct for:

- **Receiving a peer's completion report when the next substantive action is a fresh dispatch to a DIFFERENT peer.** The completion is in your inbox; the new dispatch is the substantive response, not an ack of the prior peer.
- **Internal coordinator state updates** (TaskUpdate, plan-doc edits) that don't change any peer's plan.
- **A peer's "proceeding unless you flag otherwise" interpretation where you have no flag.** Absence IS the correct semantic; the peer explicitly told you that.
- **A pure ack, thanks, or "got it"** that adds nothing the receiver needs to act on — sending it just wakes them to acknowledge your acknowledgement.

**The failure mode this codifies against:** a coordinator stays silent when a peer asks for confirmation on a stated default. That ABSENCE is the bug — the peer cannot distinguish "approved, proceed" from "not yet read"; the substantive concur should have been sent.

The protocol is asymmetric **because the cost of a missed reply is asymmetric**. Missed dispatch = peer never starts (rare now — delivery is unconditional, so this narrows to a genuinely unreachable recipient, which the platform now surfaces loudly rather than silently). Missed ACK = coordinator builds downstream on assumed-engaged peer. Missed completion = coordinator builds downstream on stale state. Missed substantive concur = peer sits on stated-default uncertainty. The wake cost of a substantive message is bounded; the cost of leaving substance unsurfaced is unbounded — so when in doubt, send it, and reserve silence for content that is genuinely pure acknowledgement.

### Failure modes the protocol addresses

| Failure | What happens | Discipline that closes it |
|---|---|---|
| No demand-ACK | Can't tell engaged-but-working from silently-stuck | B. demand ACK |
| No request-reply on completion | Coordinator catches completion only on next operator turn or 20-min cron; downstream dispatches block | C. request a reply |
| Silent ACK to a coordinator gating other dispatches | Coordinator + all gated peers all idle | B. demand ACK, even on "still working" status |
| Silent flip of a prior asserted position | Coordinator builds downstream on the old position | C. request a reply on corrections |

### Cross-references

- `knowledge_bases/agent_messaging_plugin/03_inter_agent_messaging.md` — the platform mechanism (unconditional delivery, delivery-outcome vocabulary, optional marker stripping).
- This article — the coordinator-side discipline that uses the mechanism.

The dispatch/ACK/milestone discipline has held throughout long autonomous multi-peer campaigns — many parallel peers, repeated design iterations, bug investigations, and blue-green cutovers running unattended for hours — with no silent-stuck cases requiring operator rescue.

---

## 9. Worked example — right and wrong

**Wrong (failure F1):** The coordinator dispatched an architecture-design task to a peer at session start. The dispatch brief said only "design the component." No `expected_path` declared. No watchdog scheduled. The dispatch sat for hours before the coordinator opportunistically pinged. The peer's response when pinged was honest ("still in progress, with a gap on X") — but the **gap was the unmonitored window**, not the peer's pace.

**Right (this discipline):** The same dispatch with:

```
expected_path: <repo-relative path the peer will produce>
expected_completion_signal: peer_send with section count + advisor call result
```

…followed by `execute_in_seconds(seconds=1800, memory_tag="dispatch:watchdog:<task_id>", content=<wake-up brief>)`.

At T+1800s the coordinator wakes on the memory tag:

- existence-check the declared `expected_path` on disk → file absent.
- `peer_inbox()` since dispatch time → no completion message.
- status-check `peer_send` to the peer: "status?"

The 2-hour unmonitored gap becomes a 30-minute monitored gap. The coordinator regains control of the in-flight workstream within one operator-attention-window.

---

## 10. When NOT to schedule a watchdog

Watchdogs are for **dispatches that the coordinator is on the hook to complete**. Some dispatches don't qualify:

- **Ack of a peer's completion report.** The dispatch is one-way; nothing to track.
- **FYI peer_send.** No completion is expected.
- **Operator-driven cycles** (e.g., a homunculus re-birth). The operator paces; the coordinator responds. Schedule a watchdog only if the coordinator is the one driving a sub-step inside the cycle.
- **Long-running operator-paused dispatches.** If the operator says "wait on me before continuing," don't schedule a watchdog that will ping the operator (the operator isn't in `peer_list`).

The discipline scopes to **coordinator-issued, peer-bound dispatches with a deliverable**. That is where F1 bit and that is where the watchdog earns its cost.

---

## 11. Related platform mechanisms

- `service_interface::scheduling_service::execute_in_seconds` — the underlying primitive for the per-dispatch watchdog (§3). One-time wake-up keyed by memory_tag, with optional `content` for one-step stash.
- `service_interface::scheduling_service::create_cron_schedule` — the underlying primitive for the 20-minute active-management cron (§7). Recurring wake-up at a cron expression, keyed by memory_tag.
- `service_interface::scheduling_service::ensure_global_heartbeat` — the always-on liveness case (different pattern; not for in-flight dispatch tracking).
- `service_interface::memory_service::get_memories_by_tag` — retrieve in-flight dispatches for the K=3 sweep (§6) and the cron sweep (§7).
- `service_interface::scheduling_service::clear_scheduled_actions_by_tag` — cancel a watchdog when the dispatch completes before the timer fires; cancel the active-management cron on handoff or quiescence (§7).
- `mcp__<server-name>__peer_inbox()` — read completion messages (delivery already woke the coordinator once; this is the backfill/catch-up view).
- `mcp__<server-name>__peer_send` — the dispatch / ACK / milestone mechanism (§8); delivery is unconditional, so this is the wake mechanism regardless of prose.

See also:

- `knowledge_bases/agent_messaging_plugin/03_inter_agent_messaging.md` — the delivery-contract platform mechanism (unconditional delivery, delivery-outcome vocabulary, optional marker stripping) that §8's coordinator discipline relies on.
- `responsiveness_and_checkins.md` — the same wake-up pattern applied to user-facing latency and check-ins.
- `scheduling_memory_driven.md` — the underlying scheduling philosophy (wake-ups, not payloads).

The discipline depends on four coordinator habits: (1) **never dispatch without declaring `expected_path` + `expected_completion_signal` + `kb_search_required` + `ledger_search_required`** (§2); (2) **never dispatch without scheduling a per-dispatch watchdog** (§3); (3) **while actively managing in-flight work, run the 20-minute active-management cron** (§7); (4) **dispatch, DEMAND an ACK before work starts, and REQUEST a reply at milestones / blockers / completion** (§8) — delivery is unconditional, so use the channel deliberately rather than for acks. Together they convert the dispatch chain from fire-and-forget into structurally-tracked.
