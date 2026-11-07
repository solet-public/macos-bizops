# coordination-hooks — test suite

Every security-relevant claim in `SECURITY.md` is executable. Run them:

```
python3 tests/run_all.py
```

from the plugin directory. Expected output ends with `SUITE PASSED`.

## What it needs

Only the two interpreters the plugin itself requires — `python3` and `node`.

It does **not** need a repository, a network connection, any environment
variable, or any configuration. Nothing is installed. Nothing is written
outside a temporary directory that is removed when the run finishes. The suite
is self-contained inside this plugin directory: no test resolves a path outside
it, and `manifest_consistency_smoke.py` asserts that property so it cannot
regress.

No test framework is used. The harness is Python standard library and drives
the Node hooks as subprocesses — which is also the contract Claude Code uses to
invoke them, and the only contract the Node hooks have, since they export
nothing and are top-level scripts. Adding a test framework would have put a
third-party dependency inside the artifact under review, contradicting the
supply-chain claim these tests exist to defend.

## The files

| File | Proves |
|---|---|
| `manifest_consistency_smoke.py` | The documents match the tree, and the source never names a network, file-write, or second-subprocess primitive. Static analysis of the shipped files. |
| `reminder_hooks_smoke.py` | The three reminder hooks inject fixed literals, are default-off, cannot block a session, and cannot be restructured by a hostile session label. Black-box, by execution. |
| `wake_waiter_smoke.py` | The one privileged hook conveys a single bit, never relays the child command's output, and never traps a session on any failure path. Black-box, against stub CLIs. |
| `run_all.py` | Runs all of the above; one exit code. |
| `_harness.py` | Shared process runner. Builds each child environment explicitly so a "disarmed" test cannot inherit a variable that silently arms the hook. |

## One thing the suite deliberately does not do

**The source-level checks in `manifest_consistency_smoke.py` are not a syscall
trace.** The no-network, no-file-write, one-subprocess and supply-chain
assertions prove the code never *names* the primitive. That is what makes
those claims auditable by reading, and reading is how they should be
confirmed; the test keeps them from drifting.

## Reading a failure

Every check prints `FAIL <label>: <detail>` and the run ends with a count. The
labels name the claim, not the code, so a failure reads as "which promise in
`SECURITY.md` is no longer true" rather than "which line changed".

## If you add a hook

`manifest_consistency_smoke.py` will fail until you have added it to the
`HOOK_KEYWORDS` table there and mentioned it in `hooks.json`, `plugin.json`,
`README.md` and `SECURITY.md`. That coupling is intentional: this plugin has
twice shipped a documentation claim that a newly added hook had already
falsified, and both times a human caught it by reading.
