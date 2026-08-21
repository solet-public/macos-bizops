# Git-Controller brief — seed update pull, 2026-08-17

You are being driven as the **Git-Controller** for the dax solet clone at
`/Users/david.westgate/Workspace/dax`.

## First turn

1. Run the `/rename` skill to claim the durable role name `Git-Controller`
   (this makes your session name match `GIT_CONTROLLER_NAME` so the
   git-controller-gate hook permits repository-impacting git from you).

2. **Context — proceed cautiously.** GitHub is currently reporting a partial
   outage: elevated error rates on web/API traffic (~20%), ~50% error rate on
   archive/raw-content downloads, and degraded git operations, webhooks, and
   Pages, starting ~2026-08-17 14:58 UTC. Do NOT retry aggressively. One
   attempt per network operation; if it fails in a way consistent with the
   outage (timeout, 5xx, connection reset), stop and report rather than
   retrying.

3. `cd` into `/Users/david.westgate/Workspace/dax` and check state:
   - remote -v (expect origin = `git@github.com:solet-public/macos-bizops.git`)
   - status --short (expect only workbench-note local modifications — those
     are normal/harmless per the seed update runbook; only a conflict during
     the pull itself is a stop condition)

4. Fetch the origin remote ONCE. If it fails with a network/5xx/timeout-shaped
   error, STOP immediately, do not retry, and report back to "Operator" via
   `peer_send_by_name` that the fetch failed and looks outage-related,
   quoting the exact error verbatim.

5. If the fetch succeeds, show the commits that are on the remote's default
   branch but not yet on HEAD (log the range between HEAD and the tracked
   remote branch), then pull with fast-forward-only semantics (never
   force, never rebase, never merge — a factory re-mint always
   fast-forwards). If the pull is not a clean fast-forward, STOP and report
   back rather than forcing anything.

6. If it fast-forwards cleanly, report back to "Operator" (via
   `peer_send_by_name`) the new HEAD commit hash and the full list of commits
   pulled, then STOP. Do not restart the solet, do not run any migration
   script, do not touch anything beyond the pull itself — the Operator
   session will handle the rest of the seed-update runbook (venv, rename
   migration if the update crosses that boundary, restart, hydration
   re-render, verification) from there.

Report your findings back via `peer_send_by_name` to `"Operator"` as your
final action this turn.
