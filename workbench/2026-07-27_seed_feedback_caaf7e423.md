# Seed Feedback — commit `caaf7e423` (bundle `bizops_standard`)

Written 2026-07-27 by the driving Claude Code session of the operating
homunculus `dax`, on the operator's request, while doing real bizops work
(a Marketo/Salesforce duplicate-Person cleanup) — i.e. this is *operating*
feedback, not birth feedback. **Intended for the seed authors.** Follows the
established `workbench/…_seed_feedback_*.md` convention (prior reports:
`_dax_setup_feedback.md` at the Workspace root for seed `9f55e5c3`,
`workbench/2026-07-22_seed_feedback_ca9fe1fdc.md`, and
`workbench/2026-07-26_seed_feedback_a01575d31.md`).

One item below (Issue 1) is **already fixed in this commit** — the fix and its
test ship alongside this note. The rest are recommendations.

## Operator's own words (the requests that produced this file)

> "We do not take shortcuts and we always try to fix things when we find they
> are broken: boy scout rules."

> "Fix it, and then push these and any other notes you can grab about setup
> friction and suggested improvements back to the seed repo from which we copied
> Dax. We definitely need to include instructions to update the claude.md file
> to make sure that Claude always as a first step checks Dax."

## Summary

Operating `dax` (single session, MCP-blocked shop, no-MCP `dax` command) is
solid. Connecting the two connectors this task needed — `salesforce_plugin` and
`g_suite_plugin` — surfaced one real bug and two documentation/UX defects, all
in the **first-use credential/connect flow**. There is also a behavioral defect
(Issue 4): the Step Zero "search the homunculus first" contract, though present
in `CLAUDE.md`, was not forceful enough to actually stop the driving agent from
improvising — the operator had to catch it. That last one is the operator's
headline for this round and drove the `CLAUDE.md.template` change in this commit.

These are complementary to the 2026-07-26 report's "invert the runbook from
offer-desk to wizard + add a completion checklist" recommendation, not a
restatement of it.

---

## Issue 1 — BUG (FIXED IN THIS COMMIT): g_suite `start_interface` reports success before the socket binds

**Severity:** High. The failure is silent and its symptom is baffling.

**What happened:** `start_interface` was dispatched for the local OAuth callback
server on a port that another process (the sibling `bizops-knowledge-base`
service) already held. The verb returned `{status: completed, port: 8765,
"OAuth callback server started"}`. Nothing in the result or the profile log
indicated a problem. The operator approved Google consent, the browser was
redirected to `127.0.0.1:8765`, and hit **the other process**, which returned a
bare `Not Found`. Tokens never vaulted; every Workspace verb stayed
`gsuite.not_connected` — with no error anywhere pointing at the port collision.

**Root cause:** `oauth/http_server.py::OAuthServer.start()` set the
`self._started` Event *before* `server.serve()`, inside the worker thread, then
returned the port unconditionally after a short wait. A uvicorn bind failure
(`[Errno 48] address already in use`) raised inside `serve()` and was swallowed
by a bare `except Exception` that only logged — and on this platform the bind
failure actually propagates as a `SystemExit`, which an `except Exception` would
miss entirely. Net: "started" was set true regardless of whether the socket
bound. This directly violates the platform "fail fast and loudly — no silent
fallbacks" standard.

**Fix (this commit):** `start()` now constructs the uvicorn `Server` on the
calling thread, blocks until `server.started` flips true (uvicorn only sets that
after `create_server()` binds), captures any startup exception via
`except BaseException` (so `SystemExit` is caught too), and on failure releases
the reserved port and raises a typed `OAuthServerStartError`. `plugin.py::
start_interface` catches it and returns the new typed error
`callback_server_start_failed` with the host:port and cause. Verified with a
standalone test: a pre-bound port now raises `OAuthServerStartError` (and the
port is released), a free port starts and serves the real callback route
(`400 oauth_state_invalid` JSON from our handler), then stops cleanly. Files:
`plugins/g_suite_plugin/src/g_suite_plugin/oauth/http_server.py`,
`.../plugin.py`, `.../constants.py`.

**Also recommend (not done here, needs its own design):** `start_interface`
could pre-check the port and, if the operator passed no explicit port,
auto-select a free one rather than failing — but failing loudly is strictly
better than the old silent success, so that is the floor this commit sets.

---

## Issue 2 — DEFECT: local OAuth callback defaults/examples use `localhost`/`0.0.0.0`, which break on macOS IPv4/IPv6

**Severity:** Medium–High (silent, and it is the *default* local path).

**What happened:** Even after the port collision (Issue 1) was resolved, the
first working server bound IPv4 `127.0.0.1`, but the address-book `redirect_uri`
followed the docs' example — `http://localhost:<port>/oauth/google/callback`.
The browser resolved `localhost` to IPv6 `::1` first, where nothing listened, so
the Google redirect hit "can't be reached" and the code never arrived. Switching
the redirect_uri to `http://127.0.0.1:<port>/…` fixed it immediately.

**Root cause:** `plugin.py::start_interface` defaults `host` to `0.0.0.0` (a
cloud/ALB default) for *all* deployments, and the local-setup docs
(`g_suite_plugin/knowledge_base/01_g_suite_overview.md` Stage 2 and
`hydration_guidance.md`) show the local `redirect_uri` with `localhost`. On
macOS, `localhost` commonly resolves to `::1` before `127.0.0.1`, and an
IPv4-only bind (`0.0.0.0`/`127.0.0.1`) is then unreachable via `localhost`.

**Recommended fix:** For the local / Desktop-app path, make the callback host
default to `127.0.0.1` and make every local `redirect_uri` example use
`http://127.0.0.1:<port>/oauth/google/callback` (not `localhost`). Keep
`0.0.0.0` + the HTTPS FQDN for the cloud/ALB path only. Add a one-line caveat in
the overview's redirect step: "use `127.0.0.1`, not `localhost` — on macOS
`localhost` can resolve to IPv6 `::1` and miss the IPv4 callback socket."

---

## Issue 3 — DEFECT: the address-book `register` examples in "not configured" errors omit the required per-entry `description`

**Severity:** Low–Medium (pure friction, but every first-time connector setup
hits it).

**What happened:** Both `salesforce_plugin` (`sf.not_configured`) and
`g_suite_plugin` (`app_config_error` for a missing `google_oauth_app`) return a
helpful "register it like this" example. Copy-pasting the example's `entries`
verbatim fails: `default_address_book_plugin` rejects each entry with
`address_book.invalid_entry: Entry N missing required fields: field_type,
description, value`. The examples show only `field_type` and `value`.

**Recommended fix:** Add `"description": "…"` to every entry in those example
payloads (in the plugins' `constants.py`/error-message builders, and any
hydration doc that reproduces the shorthand), so the documented example is the
one that actually validates.

---

## Issue 4 — DEFECT (behavioral): Step Zero is present but not forceful enough to stop the agent improvising

**Severity:** Medium–High. This is the operator's headline for this round.

**What happened:** Handed a real bizops task, the driving agent began by
improvising directly against the Salesforce CLI and the g_suite plugin
internals, and only ran `knowledge_service::search` after the operator asked,
"Are you consulting Dax to make sure you understand how we work?" The canonical
answers (operator-collaboration craft, decision-brief convention, the connector
setup runbooks) all existed and were excellent once queried — the agent just
didn't query first.

**Root cause:** `CLAUDE.md`'s Step Zero contract is correct but is positioned as
the second heading, below the identity paragraph, and phrased "before responding
to any question or starting any non-trivial task." That reads as advice, not as
a hard first action, and it lost to the momentum of a concrete task. This is the
same class of defect as the 2026-07-26 report's "offer-desk spine" — docs that
describe the right behavior in language soft enough to skip.

**Fix (this commit) + recommendation:** Added a top-of-managed-block
**"🛑 FIRST ACTION, EVERY SESSION — NON-NEGOTIABLE"** banner to
`plugins/github_midwife_plugin/knowledge_base/hydration_templates/
CLAUDE.md.template` (and applied it to this instance's rendered `CLAUDE.md`),
stating that a `knowledge_service::search` is the literal first step of every
task — including tasks that look like plain code/shell/config and tasks where
the agent thinks it already knows — and that answering from source, assumptions,
web, or MCP before searching the homunculus is a defect. Recommend the seed
authors keep this banner as the first element of the managed block, and consider
having the hydration completion checklist (2026-07-26 report) assert its
presence.

---

## What worked (keep it)

- **No-MCP `dax` operation is excellent.** `dax search` → `dax schema` → `dax
  call` is a clean discovery loop, and `knowledge_service::search` returns full,
  correct article content (the operator-communication governance articles drove
  real behavior change this session).
- **`salesforce_plugin`'s own overview doc was accurate and load-bearing.** The
  "install the standalone `sf` bundle, don't use npm-on-ambient-Node, pin
  `sf_cli_path`" guidance (Stage 1) was exactly right — the LaunchAgent PATH does
  not carry an nvm `sf`, and the standalone bundle shares the existing `~/.sf`
  auth, so no re-login was needed.
- **`g_suite_plugin`'s runbook was detailed enough to drive the entire OAuth
  wizard** (consent screen → Desktop client → agent-blind secret via
  `vault_service::store_from_file` → address-book entry → `start_interface` →
  `connect_account`) with only browser acts left to the operator. The two
  defects above are at the edges of an otherwise complete flow.

## Priority for the seed authors

1. **Take the Issue 1 fix** (already in this commit) and consider the
   auto-free-port enhancement.
2. **Fix the local IPv4/`127.0.0.1` default + examples (Issue 2)** — silent and
   on the default path.
3. **Keep the CLAUDE.md FIRST-ACTION banner (Issue 4)** and fix the
   register-example `description` omission (Issue 3).
