# Part 45 — feedback round from dax

Single-item round; no parent issue needed.

**FILED 2026-08-17** as `solet-public/macos-bizops` issue **#17** (label:
defect), via `gh` CLI with the filing-note convention; re-verified at HEAD
`f4147c9` immediately before filing.

---

## §45.1 — DEFECT: the shipped AskUserQuestion default-deny (settings.json + claude-session-overlay.json) is silently inert under a common enterprise Claude Code policy setting  [label: defect]

**Item number:** §45.1

**The exact command or action:** In a long-lived interactive `claude --name
Operator` session in this deployment, the assistant called the built-in
`AskUserQuestion` tool.

**Observed output:** The tool rendered its interactive multiple-choice picker
and held the session until a human manually rejected it — reported as
recurring, despite this deployment configuring the deny twice over: the
operator's global `~/.claude/settings.json` carries `"permissions": {"deny":
["AskUserQuestion"], "defaultMode": "bypassPermissions"}`, and
`client/bin/claude-dax` additionally passes `--settings
client/claude-session-overlay.json` (content `{"permissions": {"deny":
["AskUserQuestion"]}}`) per this release's own fleet-launcher doctrine
(operator ruling 2026-08-14, "AskUserQuestion... denied by default").

**Expected behavior:** Per that same doctrine — and per Claude Code's own
documentation, which states deny rules apply in every permission mode
including `bypassPermissions` and are evaluated before allow rules — the tool
should have been denied outright with nothing reaching a human.

**Root cause, confirmed against upstream Claude Code documentation (not
inference):** This operator's machine carries an enterprise-managed Claude
Code policy file with `"allowManagedPermissionRulesOnly": true`. Per Claude
Code's `permissions.md`: when that flag is `true`, user- and project-level
`allow`/`ask`/`deny` rules **do not apply at all** — only rules defined
inside the managed settings file itself are honored. The managed file's own
`permissions.deny` list here covers only a couple of `Bash`/`Read` patterns;
it does not mention `AskUserQuestion`. So both of this deployment's deny
rules — the operator's own `settings.json` entry and the
`claude-session-overlay.json` the launcher passes — are silently dropped
before permission evaluation ever reaches them. (An earlier line of
investigation on our side wrongly suspected a stale, un-reloaded session;
Claude Code's docs confirm permission settings reload live in-session, so
that was ruled out — this is a hard settings-precedence override, not a
timing issue, and would reproduce identically in a brand-new session.)

**What it costs us:** The fleet-launcher session-configuration doctrine
documents `permissions.deny` as *the* mechanism for the AskUserQuestion
default-deny ("`AskUserQuestion` is a permission-gated tool name, so a
standard permissions deny works") without qualifying that this mechanism is
wholly inoperative on any operator machine with
`allowManagedPermissionRulesOnly: true` set — which, per Claude Code's own
docs, is exactly the kind of setting an organization's IT-managed policy sets
to lock down permission customization. There is no local signal when this
happens: the picker just fires as if no deny existed, and the operator has
to independently discover their own managed-policy file to learn why a
documented, doubly-configured deny isn't working. This is the same silent
policy-driven feature-loss shape already reported in `§43.1`
(`strictPluginOnlyCustomization` silently stripping worker hooks) — a
different policy key defeating a different feature, same missing signal.

**Suggested outcome (method is upstream's call):** Either document this
mechanism's dependency on `allowManagedPermissionRulesOnly` being unset/false
so an operator on a locked-down machine knows in advance it won't work, or
have the launcher/doctrine detect the managed flag at spawn/launch time and
say so loudly instead of shipping a deny that silently does nothing. We do
not have a fix to suggest for the underlying restriction itself — from this
deployment's position that setting is enterprise-managed and out of scope to
change — only for making its effect on this specific doctrine visible.

**Local fix or divergence carried:** None available to us — the operator's
managed policy is out of our control. The only reliable mitigation on our
side is behavioral: the assistant simply never calls `AskUserQuestion` in
this deployment, regardless of what the permission config claims.

**Release you verified on:** originally verified at HEAD `68adf8b` (seed
bundle `e592c67`) / RELEASE_NOTES.md 2026-08-14 release — the release that
introduced the AskUserQuestion default-deny doctrine this item is about.
Re-verified 2026-08-17 at HEAD `f4147c9` (merged re-mints through `e934bb4`,
RELEASE_NOTES.md 2026-08-17 release) before filing: the overlay
(`client/claude-session-overlay.json`) and the doctrine's "standard
permissions deny works" mechanism section are unchanged, and the managed
policy (`allowManagedPermissionRulesOnly: true`, no `AskUserQuestion` in the
managed deny list) is still in force on this machine — the defect stands
as written.

**Content gate:** ticked after re-reading this text.
