Tags: coordinator, multi-agent, supervision, blockers, completion, evidence
Article Layer: 2

# Coordinating Work Between Sessions

A solet runs sessions; a caller chooses the work, its workspace and how to track
it. Start a session with the runtime, model, effort, workspace, first message,
permissions and an opaque label appropriate to the task. Project-specific work
records and repository custody belong to the project system that owns them.

Give a collaborator a bounded objective, relevant context, scope, evidence of
done, and a way to report progress or a blocker. Search the knowledge base and,
when prior work matters, the session ledger before implementation. A successful
search with no relevant result is a knowledge gap, not a convention to invent.

A transport receipt confirms routing or persistence; it does not establish
that a session understood or completed the work. Distinguish session liveness,
uptake, progress, completion evidence, independent acceptance and any later
landing or deployment. Read the relevant state and evidence before claiming a
transition. A blocker should identify the decision needed, its evidence and the
person or session able to decide it. Continue independent work where possible.

Use the solet-scoped peer bridge to route substantive decisions and reports.
Avoid acknowledgement loops. A coordinator should follow up on work that needs
attention and report uncertainty instead of inferring progress from a heartbeat.
When work is complete, report the result and remaining obligations promptly;
the session that started a worker handles its retirement or termination and
checks the returned host and registration details. Elapsed time alone does not
prove completion or stop a session.

Repository rules govern changes to shared checkouts. A project may keep its own
work records, evidence and landing procedure; the solet's session interface does
not require a particular project system.

For peer routing and delivery outcomes, see
`knowledge_bases/agent_messaging_plugin/03_inter_agent_messaging.md`.
