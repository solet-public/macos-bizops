# Part 49 — feedback round from dax

**FILED 2026-08-21** as `solet-public/macos-bizops`
[#33](https://github.com/solet-public/macos-bizops/issues/33). Single-item
round, no parent issue. Filed via `gh issue create --title/--body-file`
(operator standing instruction from §47.8, not `--web`).

**Release this round is measured against:** HEAD `2329ea4` (seed re-mint merge
dd4109d..9e595f4) / RELEASE_NOTES.md 2026-08-19 release — same release Part 48
was measured against; nothing has landed upstream since.

**Round thesis:** one item. The seed hydrates a per-operator launcher
(`client/bin/claude-dax`) that execs the `claude` binary directly, but ships
no check that the CLI itself is current, and no warning that more than one
install of it can silently coexist on the same machine with only PATH order
deciding which one actually runs. This operator hit both halves of that gap
on a real machine and fixed it locally; offering the fix upstream so it ships
by default rather than being rediscovered per clone.

**Items carried forward (not re-argued here):** Part 43 (#7) with §43.2 (#9)
open; Part 46 (#14) with §46.2 (#16) open; Part 47 (#18) with all eight
children (#19–#26) open; Part 48 (#27) with all five children (#28–#32) open.

**Content gate:** run against the drafted body below. The absolute filesystem
paths gathered as evidence (`claude doctor` output, `which -a claude` output)
carried the operator's own username in the home-directory prefix — generalized
to `~/...` before drafting. No employer or business specifics apply to this
topic. No credentials or secret-looking values were part of the evidence.

---

## §49.1 — Feature request: launcher hydration should keep the Claude Code CLI itself current, and flag the native-vs-Homebrew dual-install trap [label: feature-request]

### Item number
§49.1

### The outcome you need
After hydration, a launcher-started session should always run a current
Claude Code CLI without the operator having to notice a package-manager nag
and act on it by hand. Separately, if more than one install of the CLI can
exist on the same machine at once, hydration (or the launcher itself) should
make that visible rather than letting PATH order silently and invisibly pick
a winner.

### The workflow that hit the gap
Every time this operator opens a session, it's through the hydrated launcher
(`client/bin/claude-dax`), which does `exec claude ...` directly — there was
no update check anywhere in that path. Homebrew's own "Update available! Run:
brew upgrade claude-code@latest" nag kept reappearing, and investigating it
surfaced a bigger problem than a missed update: two installs of the CLI
existed on the machine at once — a Homebrew cask and an older copy installed
by the native install script — and PATH order (`/opt/homebrew/bin` ahead of
`~/.local/bin`) was silently choosing the cask. The native copy had frozen at
an old version for weeks because it wasn't the one actually running, so its
own background self-updater never had a reason to fire, and nothing surfaced
that it had stopped.

`claude doctor` made this harder to diagnose, not easier: it reported
`Config install method: native` while the binary actually in effect
(`Package manager: homebrew`, path under `~/.../Caskroom/claude-code@latest/...`)
was homebrew-managed — a stale/mismatched self-report rather than a clear
"you have two of these" signal.

This isn't a one-off for this machine — the operator's own framing raising it
was "everyone is getting confused about the proper setup," i.e. this is a
recurring source of confusion, not a single misconfiguration.

### What it costs you today
Low-grade but recurring: every reappearance of the nag costs a moment of "is
this the channel I should even be using?" with no authoritative answer
available locally. The stale native install had silently accumulated ~700MB
of dead binary versions with no cleanup trigger. The sharper cost is a false
sense of correctness: once an update check exists, "the launcher says it's
current" and "the binary actually running is current" can silently diverge
the moment PATH order or shell setup changes — that's worse than having no
check at all, because it looks green while being wrong.

### Your current workaround
Diagnosed by hand (`claude doctor`, `which -a claude`, `brew info` against
both cask variants), picked the channel already in effect (Homebrew) and
removed the other install, then wrote the check directly into this
deployment's own copy of `client/bin/claude-dax` since the hydrated version
shipped with nothing here. Every operator hydrating this seed with a
Homebrew-managed CLI presumably has to rediscover this same gap independently
unless it ships by default.

### Implementation sketch (optional appendix)
What this operator built locally, offered as a starting point rather than a
prescription — the maintainers should own the actual method:

- Resolve which install is active by following the `claude` binary's real
  path (e.g. `command -v claude` resolved through symlinks), not by guessing
  a package name — this also doubles as the "is this even a package-manager
  install" signal, since a native/npm install has no `Caskroom` segment in
  its path and self-updates on its own; the launcher should say so rather
  than silently doing nothing.
- Split the check by cost so it never delays reaching a prompt: a cheap,
  local, no-network "is it outdated" check on every launch; the actual
  upgrade only runs — in the foreground, before the CLI is exec'd — when that
  check says it's genuinely outdated. Deliberately not backgrounded:
  upgrading in the background while the session keeps running can unlink the
  install directory the just-launched session's own binary is executing
  from.
- Only the slow part — refreshing the package manager's own metadata cache —
  is backgrounded, and throttled via a timestamp file, since that part only
  needs to be fresh enough for the *next* launch's cheap check, never this
  one.
- Separately from the update check itself: hydration could detect and warn
  about a coexisting second install of the CLI (native binary present *and*
  a package-manager install present, or vice versa) as its own signal,
  independent of whether either one is currently outdated — that's the actual
  root cause here, and an update-freshness check alone doesn't fully resolve
  it if a second, unmanaged install is still sitting on PATH somewhere.

### Content gate
- [x] Re-read this issue as drafted above. No personal identifiers, no
  employer or business specifics, no credentials or secret-looking values —
  including inside the workflow description.

---

## Filing plan (proposed)

Single issue, form 03 (feature request), no parent. Would file via
`gh issue create --title/--body-file` (per the operator's standing
instruction from §47.8 — scripted, not `--web`) once the operator confirms.
