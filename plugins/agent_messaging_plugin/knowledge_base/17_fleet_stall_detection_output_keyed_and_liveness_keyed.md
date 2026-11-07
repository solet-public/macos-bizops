# Fleet Stall Detection: Output-Keyed and Liveness-Keyed Nets

Tags: knowledge:tag:plugin_reference, knowledge:tag:fleet_stall_detection, knowledge:tag:agent_messaging

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:operator-runbook, evidence-category:invariant-contract, domain:agent-messaging, domain:fleet-liveness

Embedding Description: Why this plugin carries two independent stall nets rather than one, what each can and cannot see, how the output-keyed fleet sweep differs from the liveness-keyed single-seat watcher in signal, scope, runtime coverage and authority, and which one to reach for when the fleet looks wrong.

## Purpose: two nets, two different questions

Use this article when the fleet looks wrong and you need to know which net
would have caught it, or when you are tempted to extend one of them into the
other's territory. The plugin carries two autonomous stall nets. They are not
redundant and neither supersedes the other, because they answer different
questions:

- `seat_idle_watcher` asks **"has this one seat gone quiet?"** It is
  liveness-keyed and single-seat.
- `fleet_stall_sweep` asks **"has the fleet stopped producing while it still
  holds work?"** It is output-keyed and fleet-wide.

A fleet can fail either test without failing the other, which is the whole
reason both exist.

## The four axes they differ on

**Signal.** The watcher reads a liveness signal: `compute_idle_seconds`
returns `0.0` unless a session-status file literally says `idle`, so its clock
only starts when something else has already declared the seat idle. The sweep
reads an output signal: the register artifacts the fleet's owners actually
created inside a window. The consequence is sharp and was measured. A fleet
that is alive, holding leases, and emitting nothing is not idle, so it is not
a stall to the watcher at all; it is exactly a stall to the sweep.

**Scope.** The watcher resolves one `--role-tag` to one session file and acts
on that one seat. The sweep enumerates every non-terminal session in the
`managed_session` ledger and classifies the fleet as a whole. A multi-seat
fleet has no watcher coverage beyond the single seat each watcher process was
launched for.

**Runtime coverage.** The watcher's identity path is Claude-specific by
construction: it globs `~/.claude/sessions/*.json`, requires a `sessionId`
from that file, and keys its poke-cooldown state on it. A session of any other
runtime is invisible to it. The sweep reads `agent_runtime` off the ledger and
never touches a runtime-specific path, so a Codex-only fleet is covered
identically to a Claude-only one. The sweep *reports* runtime composition and
never *predicates* on it, which is why that neutrality is measurable rather
than asserted: the same stall verdict is asserted for a Codex-only, a
Claude-only and a mixed ledger in `fleet_stall_sweep_smoke.py`.

**Authority.** This is the most important difference and the easiest to erode.
The watcher is an **actor**: it pokes or rotates a live seat, behind an
act-time rail that re-reads the pane and requires a confirmed empty composer.
The sweep is a **detector**: it reports a verdict and takes no action on any
session, imports no module that could, and has a committed audit asserting
that no drive, clear, compact, terminate or spawn call is reachable from it.
If a wake on a stall verdict is ever wanted, it belongs to a separate decision
and a separate component, not to the sweep: the moment a detector acts on live
sessions it inherits every rail an actor needs, and it stops being safe to run
unattended on a schedule.

## What each one costs you when it is the only net

The two incidents this pair exists for are worth keeping in view, because each
is the other net's blind spot made concrete.

`iss_c4fbe548`: the watcher was found dead for about 25 days. Nothing
published its last run where a reader could see it, so its silence looked
exactly like a healthy, quiet fleet. The sweep treats that as a pass
condition rather than a nicety: every completed run publishes itself, advisory
runs included, and `read_fleet_stall_sweep_status` returns that run with its
age and an explicit `silent` flag once the age passes three scheduled
intervals. One missed fire is a restart; three is the net being dead.

`iss_ebc1ce9c`: the whole fleet sat idle for 3 h 40 min with two ready queued
Units while nothing detected it and a human found it by hand. No single seat
was the failure, so no single-seat net was looking at the right thing.

## The sweep's predicate, stated once

STALLED if and only if work was ready and owned in the register AND the actors
that own it produced nothing in the window. NOT_STALLED when something was
produced, or when nothing was ready and owned — an idle fleet with no work to
do is not a stall, and reporting one would make the signal useless the moment
the queue drains. UNMEASURED when a read did not complete.

That third value matters. The sweep refuses nothing: an unreadable register
yields a named advisory and *no* verdict, never "nothing produced", because
reading a failure to measure as an absence of output would manufacture a false
stall out of a transport fault.

Both halves are register facts, read through the existing read-only CLI port
with its paging and named-refusal conventions:

- ready and owned: `db unit who-has-work` rows whose `coverage` is
  `active-owned`, the register's own name for a live, owned, unexpired Unit
  someone is supposed to be moving;
- produced: `db activity produced-work --actor <actor> --since <iso>` per
  owning actor, the artifacts that actor actually created.

## Which one to reach for

- A single named seat has stopped answering mail, or is near its cache
  boundary: that is the watcher's job, and it is the net that may act.
- Work is queued and owned and nothing is moving, or the fleet is partly or
  wholly Codex, or you want a fleet-wide verdict you can read on a schedule:
  `sweep_fleet_stall`, with `read_fleet_stall_sweep_status` to confirm the
  sweep itself is still running before you trust its last verdict.
- Neither net reports a problem but the fleet still looks wrong: check
  `read_fleet_stall_sweep_status` for `silent`, and check that a watcher
  process exists at all for the seat in question. A net nobody can tell is
  dead is the failure this article's first incident paid for.

## Extending either net

Keep the division of labour. Adding a liveness input to the sweep would
reintroduce the exact coupling it was built to avoid, and adding an action to
it would turn a scheduled detector into an unattended actor on live sessions.
Making the watcher fleet-wide would require it to stop reading a
runtime-specific session file, which is most of what it is. The honest
extensions are: new runtimes arrive in the sweep for free through the ledger's
`agent_runtime`; a new produced-work fact arrives for free through the
register's own verb; and a wake-on-stall, if it is ever wanted, is a third
component that consumes the sweep's verdict and owns its own rails.
