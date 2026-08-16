# Rotate Own Context By Delegated Drive

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:always, evidence-category:joseki, domain:session-management, domain:platform-operations


JOSEKI_KEY: rotate_own_context_by_delegated_drive
DESCRIPTION: Clear and resume the context of a session that has no manager above it, by dispatching a helper session to drive its terminal surface. Checkpoints the session's durable state, writes a resume-handoff file, spawns an inexpensive helper whose entire remit is injecting two texts — the literal /clear and a short pickup prompt — then verifies the fresh context is actively processing that prompt rather than merely cleared. Use when a driving or seat session's context window is filling and work must continue across the boundary. NOT for managed workers: a tmux-hosted worker is rotated by its manager with clear_session plus drive_session and needs no helper.
EMBEDDING_DESCRIPTION: Clear your own conversation context and pick the work back up, when you are the top session and nobody else can clear you. Save what is in flight to a handoff file, dispatch a cheap helper session whose only job is to type the clear command and then a short pickup prompt into your terminal, and confirm the fresh session is actually working on that prompt instead of sitting idle. Covers the terminal-injection mechanics and the races: a mid-turn injection queues instead of executing, and the clear can swallow the queued pickup prompt. For a session with a manager above it, use the worker rotation verbs instead.

## Input Contract

- A session whose context must be cleared and which no other session manages
- A terminal surface that can be driven: an iTerm2-hosted seat (Python API) or an equivalent injectable pane
- A drained memory projection and current workbench records — the clear destroys everything not written down
- Bindings: target_surface_selector, handoff_path, helper_model, pickup_prompt

## Output Contract

- A resume-handoff file in workbench naming what is in flight, the read order, and the single first action
- A cleared session whose durable identity and role binding are intact (neither is re-minted by a clear)
- The fresh context VERIFIED to be actively processing the pickup prompt, not idle
- The helper's transcript captured as the rotation record, and the helper terminated

## Sequence

[ ] 1. Reach a safe checkpoint before anything is destroyed
    a) Drain pending memory write-through, bring workbench records current, and confirm no peer is holding for a go-signal only this session can send [agent-executed: the clear is irreversible for anything held only in context]

[ ] 2. Write the resume handoff file
    a) Record what is in flight, the read order for a fresh context, and the ONE first action [agent-executed: a workbench file, which never ships]

[ ] 3. Dispatch the helper session
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Spawn an inexpensive helper whose brief names the target surface and the only two texts it may inject (plugin::agent_messaging_plugin::spawn_session)

[ ] 4. Helper resolves the target surface under a 0/1/N gate
    a) Enumerate sessions via the terminal's scripting API and match the target by its role variable; refuse loudly on zero or multiple matches [agent-executed: an ambiguous match must never be guessed — the wrong pane gets cleared]

[ ] 5. Helper injects the clear, submitting as a separate send
    a) Inject the literal clear command, then send the carriage return as its own distinct send [agent-executed: a newline inside the text does not submit]

[ ] 6. Helper verifies the clear took, tolerating the mid-turn queue
    a) Read the screen back and poll until the pending-input queue drains and the fresh session appears [agent-executed: injections during a turn queue rather than execute]

[ ] 7. Helper injects the pickup prompt and verifies PROCESSING
    a) Inject the short pickup prompt plus a separate carriage return, then confirm the fresh session is actively working on it; re-inject once if deaf [agent-executed: cleared-and-idle is a failure, not a success]

[ ] 8. Fresh session records the rotation and releases the helper
    a) Capture the helper's transcript as the rotation record, then terminate the helper [agent-executed: an unterminated helper is a paid idle session]

## Expected Step Count

8 steps.

## Binding Guidance

- Bind `helper_model` to an inexpensive tier. The task is mechanical injection and verification; spending a strong model on it buys nothing.
- Bind `pickup_prompt` SHORT and make it point at `handoff_path` rather than carrying the state inline. A long injection collapses into a paste chip and may not submit as typed.
- Bind `target_surface_selector` to a role-tagged variable rather than a session id captured earlier — ids go stale, and the 0/1/N gate is what makes a stale or duplicated match refuse instead of clearing the wrong pane.
- Bind `handoff_path` under `workbench/`, which never ships in a seed bundle, and put nothing in it that identifies a person, an employer, or a credential.

## Coherence Obligations

- **The clear destroys everything held only in context.** Step 1 is not a formality: anything not drained to the memory store or written to workbench is gone, and a checkpoint skipped here surfaces as a fresh session confidently redoing landed work.
- **Cleared is not resumed.** A cleared interactive session sits idle until someone types a turn, with no error anywhere — so a rotation that stops at step 6 stalls silently and indefinitely. The pickup injection IS that turn, and step 7 verifies PROCESSING rather than presence.
- **The clear can consume its own follow-up.** When both texts sit queued because the target was mid-turn, executing the clear can swallow the queued pickup prompt and leave a deaf session. One re-inject is the designed recovery; verify again after it.
- **Delegation is the mechanism, not a workaround.** A session cannot clear itself directly, and this card deliberately uses only primitives every deployment has — spawning plus terminal injection — so a seed-born solet inherits a working self-rotation. A vendor remote-control capability is explicitly not the mechanism even where one exists, because it is unavailable in exactly the managed environments that most need this.
- **Identity survives; do not re-claim reflexively.** A clear does not mint a new instance id and does not release a held role. Measure identity after the clear if it matters, but a reflexive re-registration is itself an eviction risk.
- **Never claim self-clearing is impossible.** That claim has been made and been wrong; the delegated drive is proven live.

## Next Joseki

Determined by the handoff file's stated first action — the point of the rotation is that the work continues, so the fresh context resumes the programme it was in rather than starting a new one.

## Repair Joseki

Explicitly absent as a card. Each step fails in place and is re-runnable: an ambiguous surface match refuses before touching anything, a clear that did not take is re-injected after the queue drains, and a deaf fresh session is recovered by one re-injected pickup. The only unrecoverable failure is a skipped step 1, and its repair is not a procedure — it is reconstructing lost state from the workbench and the memory store.
