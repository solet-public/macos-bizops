#!/usr/bin/env python3
"""Assert the plugin's SECURITY.md/README.md claims still describe the tree.

This is the highest-value test in the suite, because the documents ARE the
security review. The failure mode it defends has already happened twice in this
plugin's short history: adding a fifth hook falsified SECURITY.md's
"fixed string literal" claim and its hook counts, and `plugin.json`'s
description had silently omitted `wake_waiter.js` before that. Both were caught
by a human reading carefully. Neither would be caught twice.

Everything here is a source-level check on the shipped files -- an import/require
graph, an exec-form manifest walk, and prose-vs-tree consistency. It is not a
syscall trace: it proves the code does not NAME a network, subprocess, or
file-write primitive, which is what makes the SECURITY.md claims auditable by
reading. Behavioural proof of the wake waiter's one-bit claim lives in
`wake_waiter_smoke.py`, and of the reminders' fixed-literal claim in
`reminder_hooks_smoke.py`.

Run directly; exit 0 on success, non-zero on failure.
"""

from __future__ import annotations

import sys

# Must precede the _harness import: CPython caches a module's bytecode when it
# first LOADS the module, so a flag set inside _harness cannot suppress _harness's
# own .pyc. Keeping the artifact under review free of stray files wins over
# import ordering here.
sys.dont_write_bytecode = True

import ast  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_ROOT))

from _harness import (  # noqa: E402
    HOOKS_DIR,
    PLUGIN_ROOT,
    TESTS_DIR,
    Results,
    is_stdlib_module,
    preflight,
)

# The heartbeat report-alive hook was deleted; the smoke asserts its absence instead of enumerating it.
RETIRED_HEARTBEAT_HOOK = "heartbeat_report_alive.py"

# Each entry-point hook and the words a prose surface may use to name it. A new
# hook must be added here, and then every document below must mention it -- that
# coupling is the whole point of this file.
HOOK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "session_cron_sentinel.py": ("session-cron sentinel",),
    "step_zero_reminder.py": ("knowledge-base-first", "kb-first", "knowledge base"),
    "session_start_context.py": ("live session context",),
    "wake_waiter.py": ("wake waiter", "idle-wake", "wake_waiter"),
    "rotation_due_watch.py": ("rotation-due", "rotation due"),
    "capture.py": ("memory-passthrough", "memory passthrough"),
    "session_context.py": ("memory-passthrough", "memory passthrough"),
    "drain.py": ("memory-passthrough", "memory passthrough"),
    "hydrate_render.py": ("memory-passthrough", "memory passthrough"),
    "index_render.py": ("memory-passthrough", "memory passthrough"),
    "sync.py": ("memory-passthrough", "memory passthrough"),
    "headless_tool_allowlist_gate.py": ("spawn-injected",),
    "capture_session_mapping.py": ("spawn-injected",),
    "recovery_claude.py": ("recovery_claude", "working-tree recovery", "recovery saves"),
}

# R4 seed-packaging audit, Package B (2026-08-10): files that are
# entry-point-SHAPED (no underscore prefix, so _entry_point_hooks() picks
# them up) but are NOT Claude Code hooks at all -- they never fire
# automatically on any event; the agent invokes them directly via Bash, on
# its own judgment, following session_context.py's own printed
# instructions. Exempted ONLY from "must be wired in hooks.json" --
# every OTHER check (documentation, stdlib-only, no-network-unless-
# disclosed, subprocess/file-write shape) still applies in full.
AGENT_INVOKED_CLI_UTILITIES = frozenset(
    {"drain.py", "hydrate_render.py", "index_render.py", "sync.py"},
)
OPTIONAL_RUNNER = "optional_hook_runner.py"
NON_HOOK_MODULES = frozenset({"coordination_owner.py", OPTIONAL_RUNNER})

# Working-tree recovery (0.10.0). One adapter, recovery_claude.py, is a hook. Beside it ship the
# shared engine's modules: byte-identical copies of coordination_hooks_common/, listed by the
# literal MODULES tuple in the shipped working_tree_recovery.py. This suite reads THAT copy of the
# list, so it never reaches outside the plugin. Engine modules are not hooks: nothing registers
# them, and working_tree_recovery.py is also the user's restore CLI, run by hand like drain.py.
RECOVERY_ADAPTER = "recovery_claude.py"
ENGINE_FACADE = "working_tree_recovery.py"
# The engine's one place a child process is spawned. Its claim is "one Git spawn boundary".
ENGINE_SPAWN_BOUNDARY = "recovery_git.py"
# The only modules the adapter may import: the stdlib it needs and the engine facade (lazily,
# after the interpreter floor, so an old python3 exits 0 before any engine module is parsed).
ADAPTER_IMPORTS = frozenset({"__future__", "contextlib", "json", "sys", "pathlib", "working_tree_recovery"})
# Git subcommands the engine must never name: they write history, refs or packs, or reach a remote.
ENGINE_ABSENT_SUBCOMMANDS = frozenset({"pack-objects", "index-pack", "update-ref", "fetch", "push", "pull", "rebase", "gc", "fsck", "checkout", "reset", "add", "commit", "merge", "rm", "mv"})
# Subcommands that write objects or the index. They run only on an explicit user `restore`.
ENGINE_RESTORE_ONLY_SUBCOMMANDS = frozenset({"hash-object", "read-tree", "update-index", "write-tree", "commit-tree"})
ENGINE_RESTORE_MODULES = frozenset({"recovery_restore.py", "recovery_rebuild.py"})

# R4 Package C (2026-08-10): files that are entry-point-SHAPED and are ALSO
# invoked by a DIFFERENT plugin's spawn mechanism -- a spawned headless/tmux
# worker's host adapter (agent_messaging_plugin) references these by path in
# a generated Claude Code `--settings` blob at spawn time, independent of
# this plugin's own hooks.json wiring. They ship here purely as the fallback
# copy a born clone carries (rung 2 of the adapters' own resolution ladder);
# the origin checkout's `.claude/hooks/<file>` is rung 1 and the primary
# copy. As of `0.8.0` (seed feedback #40/§51.1, 2026-08-24) both are ALSO
# registered in hooks.json -- the two delivery routes are deliberately
# independent, not mutually exclusive: a managed policy's
# `strictPluginOnlyCustomization: ["hooks"]` strips the adapter's own
# `--settings` injection, and the hooks.json registration is what survives
# that policy for a plugin already in `strictKnownMarketplaces`. A session
# under a policy that strips neither gets both -- harmless by design, see
# each file's own module docstring. Kept as a named set (rather than folded
# into the ordinary "must be wired" hooks) so the check below asserts BOTH
# facts explicitly: registered here, AND still the adapter's own fallback
# copy, never one or the other.
SPAWN_INJECTED_HOOKS = frozenset(
    {"headless_tool_allowlist_gate.py", "capture_session_mapping.py"},
)

# Documents that must name every hook. hooks.json and plugin.json carry a
# one-line description; README.md and SECURITY.md carry the prose.
PROSE_SURFACES = ("hooks/hooks.json", ".claude-plugin/plugin.json", "README.md", "SECURITY.md")

# Module names that would falsify "no network I/O of any kind in any hook".
NETWORK_MODULES = frozenset(
    {
        "http",
        "https",
        "http2",
        "net",
        "dgram",
        "tls",
        "dns",
        "socket",
        "urllib",
        "urllib2",
        "urllib3",
        "httplib",
        "http.client",
        "ftplib",
        "telnetlib",
        "smtplib",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "websockets",
    }
)

# Source-level names that would falsify "No hook writes a file as an action of
# its own" for a hook NOT in FILE_WRITE_CAPABLE_HOOKS. Reads are fine and
# expected (hooks read the session file). rotation_due_watch.py is the
# DISCLOSED exception: it writes a small, secret-free throttle marker file (a
# timestamp, nothing else) under the receipt-derived identity directory -- see
# SECURITY.md's "The rotation-due watch hook" section for the full contract.
WRITE_PRIMITIVES = (
    "write_text",
    "write_bytes",
    "os.remove",
    "os.unlink",
    "os.mkdir",
    "os.makedirs",
    "shutil.",
    "tempfile.NamedTemporary",
    "os.rename",
    # R4 seed-packaging audit, Package B (2026-08-10): the memory-passthrough
    # files' own write idiom is `open(path, "w"/"a", encoding=...)`, which
    # none of the tokens above catch -- without these two, hydrate_render.py
    # and capture.py would pass this check vacuously (no primitive NAMED)
    # despite genuinely writing files directly in their own source, not via
    # delegation. Narrow on purpose: matches this codebase's own consistent
    # `open(x, "w"/"a", encoding=...)` style, not every possible open() call.
    '"w", encoding',
    '"a", encoding',
)
# Two distinct write shapes: MARKER_ONLY writes a bare timestamp and
# nothing else (rotation_due_watch.py, checked below); CONTENT_BEARING
# writes real fact content sourced from an already-authenticated export
# snapshot or the agent's own append-only journal, never held to
# MARKER_ONLY's narrower bare-timestamp claim (membership here only feeds
# the write-primitive/subprocess-shape checks above, not a content-source
# claim -- narrowing that claim precisely enough to check accurately was
# tried and dropped this pass, see the commit message's disclosed gaps).
MARKER_ONLY_WRITE_HOOKS = frozenset({"rotation_due_watch.py"})
CONTENT_BEARING_WRITE_HOOKS = frozenset(
    {
        "capture.py",
        "hydrate_render.py",
        "index_render.py",
        "_journal.py",
        # R4 Package C (2026-08-10): writes a real content record (agent_instance_id,
        # claude_session_id, captured_at, capture_source) to a file-per-firing spool,
        # not a bare timestamp -- same CONTENT_BEARING class as capture.py's journal
        # entry, never MARKER_ONLY's narrower bare-timestamp claim.
        "capture_session_mapping.py",
    },
)
FILE_WRITE_CAPABLE_HOOKS = MARKER_ONLY_WRITE_HOOKS | CONTENT_BEARING_WRITE_HOOKS

# Source-level names that would falsify "no hook outside SUBPROCESS_CAPABLE_HOOKS
# executes a subprocess." wake_waiter.py and rotation_due_watch.py are among
# the DISCLOSED exceptions -- each is checked
# against its OWN fixed-shape contract below (no shell=True, subprocess.run
# only, a fixed/bounded argv), not just membership in the set.
# "subprocess." (WITH the trailing dot), never the bare word "subprocess" --
# several of this plugin's own docstrings use the plain English phrase "a
# hook subprocess has no MCP bridge" (memory-passthrough files), which a
# bare-substring match would misclassify as a real subprocess.run/Popen call.
# Actual usage always appears as `subprocess.<method>(` somewhere in the same
# file even when `import subprocess` itself has no trailing dot, so this
# stays a true-positive-preserving, false-positive-eliminating narrowing --
# same class of fix as the wake-waiter argv-shape regex above.
SUBPROCESS_PRIMITIVES = (
    "subprocess.",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execl",
    "os.spawn",
    "os.fork",
    "pty.spawn",
    "commands.getoutput",
)
SUBPROCESS_OWNER = "wake_waiter.py"
SUBPROCESS_CAPABLE_HOOKS = frozenset(
    {
        "wake_waiter.py",
        "rotation_due_watch.py",
        "sync.py",
        "session_start_context.py",
        "session_cron_sentinel.py",
        # The seventh: its read-only git children are spawned by the engine's one spawn boundary
        # (ENGINE_SPAWN_BOUNDARY), which it calls. The adapter file itself names no subprocess
        # primitive, so check_subprocess_capable_hooks credits it through that boundary.
        RECOVERY_ADAPTER,
    },
)
# The fixed process_key each hook's subprocess.run call must carry verbatim --
# same shape-fixity property as the wake waiter's argv check below, applied to
# the solet-CLI callers. sync.py legitimately calls more than one
# process_key (export once, upsert per pending entry); its own named
# constant EXPORT_PROCESS_KEY is checked as the representative fixed value --
# proving the fixed-shape/no-shell/subprocess.run-only properties hold, the
# same bar every other solet-calling hook here is held to.
SOLET_CALLER_PROCESS_KEYS = {
    "session_cron_sentinel.py": "plugin::fleet_maintenance_plugin::session_cron_observation",
    "rotation_due_watch.py": "plugin::agent_messaging_plugin::report_context_status",
    "sync.py": "service_interface::memory_service::export_memories",
}

_NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
}

# Shell metacharacters that must never appear in an exec-form argv. `${VAR}` is
# Claude Code's own manifest substitution and is expressly allowed; `$(` is not.
SHELL_METACHARACTERS = ("$(", "`", ";", "|", "&", ">", "<", "\n")

ALLOWED_COMMANDS = frozenset({"python3"})
PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}/hooks/"

# Cadence ruling (2026-08-11, cadence-only): step_zero_reminder.py and
# check_messages_reminder.py moved from UserPromptSubmit to
# SessionStart(startup|resume|clear|compact) so they stop re-firing every prompt
# turn. The 2026-08-01 always-armed ruling for step_zero_reminder.py (no
# environment gate) is untouched by this move and stays pinned by
# reminder_hooks_smoke.py's check_step_zero_fires_everywhere.
# session_context.py was never in scope for this move (it is the
# memory-passthrough context-gauge hook, not a reminder) and keeps its
# pre-existing UserPromptSubmit + SessionStart bindings.
REQUIRED_SESSION_START_CADENCE = {
    "step_zero_reminder.py",
    "session_start_context.py",
}
# RED MUTATION: re-adding either of these two to any UserPromptSubmit entry.
FORBIDDEN_ON_USER_PROMPT_SUBMIT = frozenset({"step_zero_reminder.py", "session_start_context.py"})


def _read(relative: str) -> str:
    return (PLUGIN_ROOT / relative).read_text(encoding="utf-8")


def _read_prose(relative: str) -> str:
    """Read a document with newlines collapsed, so hard-wrapped phrases still match."""
    return re.sub(r"\s+", " ", _read(relative))


def _string_constants(source: str, filename: str) -> set[str]:
    return {node.value for node in ast.walk(ast.parse(source, filename=filename)) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _engine_modules() -> tuple[str, ...]:
    """The module names the shipped working_tree_recovery.py lists in its literal MODULES tuple."""
    facade = HOOKS_DIR / ENGINE_FACADE
    if not facade.is_file():
        return ()
    for node in ast.parse(facade.read_text(encoding="utf-8"), filename=ENGINE_FACADE).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "MODULES" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, tuple) and all(isinstance(name, str) for name in value):
                return value
    return ()


def _entry_point_hooks() -> list[str]:
    """Hook scripts Claude Code invokes directly (siblings start with '_'); engine modules are not hooks."""
    engine = set(_engine_modules())
    return sorted(path.name for path in HOOKS_DIR.iterdir() if path.is_file() and path.suffix in {".js", ".py"} and not path.name.startswith("_") and path.name not in NON_HOOK_MODULES and path.name not in engine)


def _sibling_modules() -> list[str]:
    return sorted(path.name for path in HOOKS_DIR.iterdir() if path.is_file() and path.suffix == ".py" and path.name.startswith("_"))


def _manifest_entries(manifest: dict[str, object]) -> list[dict[str, object]]:
    """Flatten hooks.json into the individual hook invocation records."""
    entries: list[dict[str, object]] = []
    hooks = manifest.get("hooks")
    if not isinstance(hooks, dict):
        return entries
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for entry in inner:
                if isinstance(entry, dict):
                    entries.append(entry)
    return entries


def _py_imports(source: str, filename: str) -> set[str]:
    tree = ast.parse(source, filename=filename)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def check_manifest_is_exec_form(res: Results, entries: list[dict[str, object]]) -> None:
    """The property the plugin exists for: no shell anywhere in the invocation path."""
    for entry in entries:
        label = json.dumps(entry.get("args", entry))[:70]
        res.check(entry.get("type") == "command", f"exec-form type: {label}", f"got {entry.get('type')!r}")
        command = entry.get("command")
        res.check(
            command in ALLOWED_COMMANDS,
            f"bare interpreter, not a shell: {label}",
            f"command={command!r} not in {sorted(ALLOWED_COMMANDS)}",
        )
        args = entry.get("args")
        if not res.check(
            isinstance(args, list),
            f"args is an array, not a string: {label}",
            f"got {type(args).__name__}",
        ):
            continue
        assert isinstance(args, list)
        for arg in args:
            if not res.check(isinstance(arg, str), f"arg is a string: {label}"):
                continue
            found = [meta for meta in SHELL_METACHARACTERS if meta in arg]
            res.check(not found, f"no shell metacharacter in arg: {arg}", f"found {found}")
        first = args[0] if args else None
        res.check(
            isinstance(first, str) and first.startswith(PLUGIN_ROOT_TOKEN),
            f"script path is plugin-root relative: {label}",
            f"got {first!r}",
        )


def _entry_hook_scripts(entry: object) -> set[str]:
    """The runner is invocation infrastructure; its target is the hook actor."""
    args = entry.get("args") if isinstance(entry, dict) else None
    if not isinstance(args, list) or not args or not isinstance(args[0], str):
        return set()
    index = 1 if Path(args[0]).name == OPTIONAL_RUNNER else 0
    if len(args) <= index or not isinstance(args[index], str):
        return set()
    return {Path(args[index]).name}


def _check_optional_target(res: Results, args: list[object]) -> None:
    valid = len(args) >= 3 and isinstance(args[1], str) and isinstance(args[2], str)
    if not res.check(valid, "optional runner has a target and event"):
        return
    target = str(args[1])
    res.check(target == PLUGIN_ROOT_TOKEN + Path(target).name, "optional target is a plugin-root script")
    res.check(Path(target).name not in {"headless_tool_allowlist_gate.py", "lane_write_boundary.py", "remote_control_guard.py"}, "security hooks are never optional targets")


def check_optional_runner_contract(res: Results, entries: list[dict[str, object]]) -> None:
    runner = HOOKS_DIR / OPTIONAL_RUNNER
    if not res.check(runner.is_file(), "optional runner exists in the plugin tree"):
        return
    source = _read_prose("hooks/" + OPTIONAL_RUNNER).lower()
    for phrase in ("never deleted or renamed", "plugin refresh", "next cutover"):
        res.check(phrase in source, f"optional runner declares lifecycle: {phrase}")
    for entry in entries:
        args = entry.get("args")
        if not isinstance(args, list) or not args or not isinstance(args[0], str):
            continue
        if Path(args[0]).name != OPTIONAL_RUNNER:
            res.check(Path(args[0]).name == "headless_tool_allowlist_gate.py", "only the security hook retains direct plugin routing")
            continue
        _check_optional_target(res, args)


def _group_scripts(group: object) -> set[str]:
    """The script basenames a single hooks.json matcher-group invokes."""
    if not isinstance(group, dict):
        return set()
    inner = group.get("hooks")
    if not isinstance(inner, list):
        return set()
    scripts: set[str] = set()
    for entry in inner:
        scripts.update(_entry_hook_scripts(entry))
    return scripts


def _group_matcher(group: object) -> str:
    matcher = group.get("matcher", "") if isinstance(group, dict) else ""
    return matcher if isinstance(matcher, str) else ""


def _bind_by_event_matcher(hooks: dict[str, object]) -> dict[tuple[str, str], set[str]]:
    """Flatten hooks.json into {(event, matcher): {script names}}.

    check_tree_matches_manifest proves each script is wired SOMEWHERE; this
    keeps the (event, matcher) grouping, unlike _manifest_entries' full
    flatten, because check_reminder_cadence's claim is specifically about
    which matcher group a script sits in, not merely that it is referenced
    anywhere in the manifest.
    """
    bound: dict[tuple[str, str], set[str]] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            key = (event, _group_matcher(group))
            bound.setdefault(key, set()).update(_group_scripts(group))
    return bound


def check_session_start_sources(res: Results, manifest: dict[str, Any]) -> None:
    """The principle-block hook is sent every source a session starts from, a compaction included."""
    matchers = [_group_matcher(g) for g in manifest["hooks"]["SessionStart"] if "session_start_context.py" in _group_scripts(g)]
    res.check(len(matchers) == 1, "session_start_context.py is bound once under SessionStart")
    pattern = "|".join(matchers)
    for source in ("startup", "resume", "clear", "compact"):
        res.check(bool(re.fullmatch(pattern, source)), f"the session_start_context.py group matches source {source}")
    res.check(not re.fullmatch(pattern, "unknown"), "the session_start_context.py group does not match an unnamed source")


def check_cron_matchers(res: Results, manifest: dict[str, Any]) -> None:
    """Both tool names belong to exactly one sentinel registration per surface."""
    groups = manifest["hooks"]["PostToolUse"]
    matches = [group for group in groups if any("session_cron_sentinel.py" in str(hook) for hook in group["hooks"])]
    root_settings = _ROOT / ".claude" / "settings.json"
    if root_settings.is_file():
        root = json.loads(root_settings.read_text(encoding="utf-8"))
        root_matches = [group for group in root["hooks"]["PostToolUse"] if "session_cron_sentinel.py" in str(group)]
        res.check(
            len(root_matches) == 1 and set(root_matches[0].get("matcher", "").split("|")) == {"CronCreate", "ScheduleWakeup"},
            "root_both_tool_matchers_registered",
            "one root optional-runner entry must match both tools",
        )
    print("REACHED both_tool_matchers_registered", flush=True)
    res.check(
        len(matches) == 1 and set(matches[0].get("matcher", "").split("|")) == {"CronCreate", "ScheduleWakeup"},
        "both_tool_matchers_registered",
        "one sentinel entry must match both tools",
    )


def check_reminder_cadence(res: Results, manifest: dict[str, object]) -> None:
    """Pin the 2026-08-11 cadence ruling directly against hooks.json."""
    hooks = manifest.get("hooks")
    if not res.check(isinstance(hooks, dict), "hooks.json has a hooks object"):
        return
    assert isinstance(hooks, dict)
    bound = _bind_by_event_matcher(hooks)

    session_start = bound.get(("SessionStart", "startup|resume|clear|compact"), set())
    res.check(
        REQUIRED_SESSION_START_CADENCE <= session_start,
        "step_zero/check_messages/role_binding reminders all fire on SessionStart(startup|resume|clear|compact)",
        f"missing {sorted(REQUIRED_SESSION_START_CADENCE - session_start)}",
    )

    on_user_prompt_submit: set[str] = set()
    for (event, _matcher), scripts in bound.items():
        if event == "UserPromptSubmit":
            on_user_prompt_submit |= scripts
    offenders = on_user_prompt_submit & FORBIDDEN_ON_USER_PROMPT_SUBMIT
    res.check(
        not offenders,
        "step_zero/check_messages carry no UserPromptSubmit binding",
        f"found {sorted(offenders)} still bound to UserPromptSubmit",
    )
    # session_context.py is deliberately NOT in FORBIDDEN_ON_USER_PROMPT_SUBMIT
    # -- it keeps its pre-existing UserPromptSubmit binding, unaffected by
    # this ruling.
    res.check(
        "session_context.py" in on_user_prompt_submit,
        "session_context.py keeps its pre-existing UserPromptSubmit binding (out of scope for the reminder-cadence move)",
        f"UserPromptSubmit carries {sorted(on_user_prompt_submit)}",
    )


def check_tree_matches_manifest(res: Results, entries: list[dict[str, object]], hooks: list[str]) -> None:
    referenced: set[str] = set()
    for entry in entries:
        referenced.update(_entry_hook_scripts(entry))

    for name in referenced:
        res.check((HOOKS_DIR / name).is_file(), f"hooks.json path exists on disk: {name}")

    for name in hooks:
        if name in AGENT_INVOKED_CLI_UTILITIES:
            res.check(
                name not in referenced,
                f"agent-invoked CLI utility is NOT wired in hooks.json: {name}",
                "this file is documented as never auto-firing -- a hooks.json entry for it would be a real behavior change, not just a classification error",
            )
            continue
        if name in SPAWN_INJECTED_HOOKS:
            res.check(
                name in referenced,
                f"spawn-injected worker hook IS ALSO wired in hooks.json: {name}",
                "this file is documented as registered here specifically so it survives a managed policy that strips a spawned worker's own adapter-generated --settings copy (strictPluginOnlyCustomization) -- a missing hooks.json entry would silently reopen seed feedback #40/§51.1 for that exact host class",
            )
            continue
        res.check(name in referenced, f"hook is wired in hooks.json: {name}", "present on disk but never invoked")

    orphans = referenced - set(hooks)
    res.check(not orphans, "no hooks.json entry points at a missing script", f"dangling: {sorted(orphans)}")

    res.check(
        set(hooks) == set(HOOK_KEYWORDS),
        "HOOK_KEYWORDS covers exactly the hooks on disk",
        f"on disk {sorted(set(hooks) - set(HOOK_KEYWORDS))}, in table only {sorted(set(HOOK_KEYWORDS) - set(hooks))}",
    )


def check_heartbeat_hook_is_deleted(res: Results, entries: list[dict[str, object]]) -> None:
    """The heartbeat report-alive hook is deleted: no registered command names it and no file ships."""
    named = [entry for entry in entries if RETIRED_HEARTBEAT_HOOK in json.dumps(entry)]
    res.check(not named, f"hooks.json registers no command naming {RETIRED_HEARTBEAT_HOOK}", f"found {named}")
    res.check(not (HOOKS_DIR / RETIRED_HEARTBEAT_HOOK).exists(), f"{RETIRED_HEARTBEAT_HOOK} is not shipped in hooks/")


def check_siblings_are_not_orphans(res: Results, siblings: list[str], hooks: list[str]) -> None:
    """A '_'-prefixed module is exempt from the manifest only if a hook imports it."""
    imported: set[str] = set()
    for name in hooks:
        if not name.endswith(".py"):
            continue
        for module in _py_imports((HOOKS_DIR / name).read_text(encoding="utf-8"), name):
            imported.add(f"{module}.py")
    for sibling in siblings:
        res.check(sibling in imported, f"sibling module is imported by a hook: {sibling}", "unreferenced file in the shipped tree")


def check_every_hook_is_documented(res: Results, hooks: list[str]) -> None:
    for surface in PROSE_SURFACES:
        text = _read_prose(surface).lower()
        for name in hooks:
            # The bounded sentinel Unit does not own package metadata. Its full
            # inventory is pinned in README, SECURITY and hooks.json instead.
            if name == "session_cron_sentinel.py" and surface == ".claude-plugin/plugin.json":
                continue
            # Naming the script file itself is always sufficient documentation;
            # the prose synonyms exist for surfaces that describe rather than list.
            keywords = (name, *HOOK_KEYWORDS.get(name, ()))
            res.check(
                any(word.lower() in text for word in keywords),
                f"{surface} names {name}",
                f"none of {list(keywords)} present",
            )


def check_security_md_counts(res: Results, hooks: list[str]) -> None:
    text = _read_prose("SECURITY.md")
    total = len(hooks)
    reminders = len([name for name in hooks if name.endswith("_reminder.py")])

    res.check(
        f"is {_NUMBER_WORDS[total]} Claude Code hooks" in text,
        "SECURITY.md hook count matches the tree",
        f"tree has {total}; expected the phrase 'is {_NUMBER_WORDS[total]} Claude Code hooks'",
    )
    res.check(
        f"{_NUMBER_WORDS[reminders]} context reminders" in text,
        "SECURITY.md reminder count matches the tree",
        f"tree has {reminders} *_reminder.py",
    )
    inert = total - len(SUBPROCESS_CAPABLE_HOOKS)
    res.check(
        f"other {_NUMBER_WORDS[inert]} hooks execute nothing" in text,
        "SECURITY.md 'execute nothing' count matches the tree",
        f"expected 'other {_NUMBER_WORDS[inert]} hooks execute nothing' ({total} total minus the {len(SUBPROCESS_CAPABLE_HOOKS)} disclosed subprocess-capable hooks)",
    )
    # Every hook except the four that are armed for every session with no environment gate: the three
    # that were (see SECURITY.md) and recovery_claude.py, which has to fire in a user's own session.
    default_off = total - 4
    # .lower() here (unlike the two checks above): this phrase is expected to
    # open its own sentence in prose ("Six of the seven..."), so a literal
    # lowercase match would wrongly fail on ordinary sentence-initial
    # capitalization.
    res.check(
        f"{_NUMBER_WORDS[default_off]} of the {_NUMBER_WORDS[total]} hooks are default-off" in text.lower(),
        "SECURITY.md default-off count matches the tree",
        f"expected '{_NUMBER_WORDS[default_off]} of the {_NUMBER_WORDS[total]} hooks are default-off'",
    )


def check_no_network(res: Results, hooks: list[str], siblings: list[str]) -> None:
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        modules = _py_imports(source, name)
        offending = sorted(modules & NETWORK_MODULES)
        res.check(not offending, f"no network module in {name}", f"imports {offending}")


def check_no_file_writes(res: Results, hooks: list[str], siblings: list[str]) -> None:
    for name in hooks + siblings:
        if name in FILE_WRITE_CAPABLE_HOOKS:
            continue
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        hits = [token for token in WRITE_PRIMITIVES if token in source]
        res.check(not hits, f"no file-write primitive in {name}", f"found {hits}")


def check_file_write_hooks_write_only_markers(res: Results) -> None:
    """The MARKER_ONLY file-writers may only write a throttle marker
    -- a timestamp string -- never anything content-bearing. Source-level:
    proves no OTHER write target exists, e.g. no writing to a path built from
    stdin/tool-input data, which would be a materially different claim than
    'writes its own fixed marker file'."""
    for name in sorted(MARKER_ONLY_WRITE_HOOKS):
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        res.check(
            "write_text(str(time.time()))" in source,
            f"{name}'s only write is a bare timestamp marker",
            "expected the fixed 'write_text(str(time.time()))' shape",
        )


def check_subprocess_capable_hooks(res: Results, hooks: list[str], siblings: list[str]) -> None:
    owners: list[str] = []
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        if any(token in source for token in SUBPROCESS_PRIMITIVES):
            owners.append(name)
    boundary = HOOKS_DIR / ENGINE_SPAWN_BOUNDARY
    if boundary.is_file() and any(token in boundary.read_text(encoding="utf-8") for token in SUBPROCESS_PRIMITIVES):
        owners.append(RECOVERY_ADAPTER)  # its children are spawned by the engine boundary it calls
    res.check(
        frozenset(owners) == SUBPROCESS_CAPABLE_HOOKS,
        "exactly the disclosed hooks can execute a subprocess",
        f"expected {sorted(SUBPROCESS_CAPABLE_HOOKS)}, found {sorted(owners)}",
    )

    for name, process_key in SOLET_CALLER_PROCESS_KEYS.items():
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        res.check(
            re.search(r"\bshell\s*=\s*True", source) is None,
            f"{name} never passes a shell option",
            "a `shell=True` option appears in the source",
        )
        res.check(
            source.count("subprocess.run") >= 1 and not re.search(r"\bsubprocess\.(Popen|call|check_call|check_output)\s*\(|\bos\.system\s*\(", source),
            f"{name} uses subprocess.run only",
            "a different subprocess-family call is present",
        )
        res.check(
            process_key in source,
            f"{name}'s solet-call argv carries its fixed process_key",
            f"expected {process_key!r} to appear in the source as a named constant",
        )
        res.check(
            re.search(r'\["solet-bridge",\s*"call",', source) is not None,
            f"{name} invokes solet via a fixed argv prefix",
            'expected the literal ["solet-bridge", "call", ...] argument vector',
        )

    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    # `shell=True` is the option that would turn a fixed argv into a shell
    # string. Match the option form, not the word -- the source says "no shell"
    # in prose, and a comment must not be able to fail this check.
    res.check(
        re.search(r"\bshell\s*=\s*True", waiter) is None,
        f"{SUBPROCESS_OWNER} never passes a shell option",
        "a `shell=True` option appears in the source",
    )
    res.check(
        waiter.count("subprocess.run") >= 1 and not re.search(r"\bsubprocess\.(Popen|call|check_call|check_output)\s*\(|\bos\.system\s*\(", waiter),
        f"{SUBPROCESS_OWNER} uses subprocess.run only",
        "a different subprocess-family call is present",
    )
    # The argv is fixed in SHAPE: literal subcommand, literal flag, and one
    # numeric element that can only come out of resolve_max_wait_s() — a
    # validated positive integer, never raw environment text. The behavioural
    # half (malformed overrides fall back loudly to the default) is pinned by
    # wake_waiter_smoke.py's bounded-wait cases.
    res.check(
        '[cli, "wake", "--max-wait", str(resolve_max_wait_s())]' in waiter,
        f"{SUBPROCESS_OWNER} argv is fixed",
        'expected the literal ["wake", "--max-wait", str(resolve_max_wait_s())] argument vector',
    )


def check_child_output_is_unread(res: Results) -> None:
    """SECURITY.md: the child's stdout/stderr are 'dropped unread'.

    This has to be a SOURCE check, not a behavioural one. Switching the spawn to
    `stdout=subprocess.PIPE` reads the child's streams into the parent --
    falsifying "unread" -- while still emitting nothing, so no black-box test
    can see it. Mutation testing found exactly this hole (against the prior
    Node implementation's equivalent `stdio: "pipe"` shape; the risk carries
    over unchanged to this hook's Python `subprocess.run` call).
    """
    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    for stream in ("stdin", "stdout", "stderr"):
        res.check(
            re.search(rf"\b{stream}\s*=\s*subprocess\.DEVNULL", waiter) is not None,
            f"{SUBPROCESS_OWNER} spawns with {stream} set to DEVNULL",
            f"{stream}=subprocess.DEVNULL not found in the source",
        )
    for attr in ("result.stdout", "result.stderr", "result.output"):
        res.check(
            attr not in waiter,
            f"{SUBPROCESS_OWNER} never reads {attr}",
            "the child's output is referenced in the source",
        )


def check_docs_name_the_bounded_wake_argv(res: Results) -> None:
    """README.md/SECURITY.md must describe the wake waiter's REAL argv.

    The failing mutation this names: the waiter's argument vector changes
    (as it did on 2026-08-09, when the bounded `--max-wait` was added) and a
    prose surface keeps describing the old shape -- README.md did exactly
    that, claiming "the single fixed argument `wake`" for three days while
    the source and SECURITY.md both already carried the bound. The expected
    tokens are DERIVED from the source literal (the same one
    check_subprocess_capable_hooks pins), so a future argv change goes red
    here until every prose surface names the new shape -- the same
    derive-from-the-wiring class as reminder_hooks_smoke.py's
    check_manifest_bound_events_echo.
    """
    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    argv_literal = re.search(
        r'\[cli,\s*((?:"[^"]+",\s*)+)str\(resolve_max_wait_s\(\)\)\]',
        waiter,
    )
    res.check(
        argv_literal is not None,
        f"{SUBPROCESS_OWNER}'s argv literal is extractable for doc coupling",
        "the argv literal's shape changed; update this check alongside the docs",
    )
    tokens = re.findall(r'"([^"]+)"', argv_literal.group(1)) if argv_literal else []
    for surface in ("README.md", "SECURITY.md"):
        text = _read_prose(surface)
        for token in tokens:
            res.check(
                token in text,
                f"{surface} names the waiter argv token {token!r}",
                f"the source argv carries {token!r} but {surface} never mentions it",
            )
        res.check(
            "AGENT_WAKE_MAX_WAIT_S" in text,
            f"{surface} names the wait-bound override variable",
            "AGENT_WAKE_MAX_WAIT_S absent",
        )
        # The stale pre-bound form closes its backtick right after `wake`;
        # the current form always continues with the bound.
        res.check(
            re.search(r"\$AGENT_WAKE_CLI wake`", text) is None,
            f"{surface} does not describe the stale unbounded argv",
            "found the pre-2026-08-09 `$AGENT_WAKE_CLI wake` (no --max-wait) form",
        )


# The one disclosed, narrow exception to "stdlib-only": rotation_due_watch.py
# imports agent_messaging_plugin.rotation_thresholds -- a zero-third-party-
# dependency SAME-PLATFORM module (not a PyPI package; ships alongside this
# plugin in every capability bundle), resolved via CLAUDE_PROJECT_DIR and
# imported inside a try/except that degrades gracefully (warns + skips the
# gauge computation) if it is ever absent. Never silently widen
# this set -- a real third-party or unbounded dependency belongs in
# SECURITY.md's Supply chain section, not just here.
ALLOWED_CROSS_PLATFORM_IMPORTS: dict[str, frozenset[str]] = {
    "rotation_due_watch.py": frozenset({"agent_messaging_plugin"}),
}


def check_stdlib_only(res: Results, hooks: list[str], siblings: list[str]) -> None:
    sibling_modules = {Path(name).stem for name in siblings}
    # AGENT_INVOKED_CLI_UTILITIES are legitimate import targets for each
    # other within this plugin (hydrate_render.py imports index_render;
    # sync.py imports drain and hydrate_render directly to avoid an extra
    # subprocess layer) -- same "local module, not a third-party package"
    # reasoning as sibling_modules above, just for the non-underscore set.
    utility_modules = {
        *(Path(name).stem for name in AGENT_INVOKED_CLI_UTILITIES),
        *(Path(name).stem for name in NON_HOOK_MODULES),
    }
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        allowed_cross_platform = ALLOWED_CROSS_PLATFORM_IMPORTS.get(name, frozenset())
        for module in sorted(_py_imports(source, name)):
            res.check(
                is_stdlib_module(module) or module in sibling_modules or module in utility_modules or module in allowed_cross_platform,
                f"{name} imports only the Python stdlib (plus its disclosed exceptions)",
                f"{module!r} is neither stdlib, a sibling module, a local utility module, nor a disclosed exception",
            )


def check_verification_section(res: Results) -> None:
    """SECURITY.md's Verification section must cite tests that actually exist.

    GTE-12: a citation may be the bare plugin-relative form (`tests/<name>.py`)
    OR a full repo-relative path ending in it -- ACCEPT BOTH, never swap one
    for the other, because the cited-path gate (a checkout-wide instrument
    this artifact does not control) resolves every backtick token repo-root-
    absolute only, while the bare form is what stays meaningful when this
    plugin is reviewed standalone with no ancestor `plugins/.../` path to
    resolve against.

    The full form is recognized ONLY by the `coordination-hooks/` boundary
    immediately before `tests/`, not by any arbitrary prefix: this table also
    carries pre-existing CHECKOUT-EXTERNAL citations one directory shape off
    from this plugin's own (e.g. `.claude/hooks/tests/...`,
    `plugins/agent_messaging_plugin/tests/...`) that legitimately point
    OUTSIDE this plugin and must never be checked against PLUGIN_ROOT. A
    prefix-agnostic `tests/<name>.py` suffix match would misfire on those --
    caught live the first time this widened (GTE-12's own build, three false
    FAILs). Requiring the plugin's own directory name as the anchor is what
    keeps "cites one of MY tests" and "cites someone else's tests/ dir that
    happens to share a filename shape" distinguishable without hardcoding
    this plugin's ancestor path (still self-contained: `coordination-hooks`
    is this plugin's OWN name, the same invariant `PLUGIN_ROOT` already
    encodes structurally, never an assumption about what sits above it).
    """
    text = _read("SECURITY.md")
    if not res.check("## Verification" in text, "SECURITY.md has a Verification section"):
        return
    raw_cited = set(re.findall(r"`((?:[\w./-]*coordination-hooks/)?tests/[A-Za-z0-9_]+\.py)`", text))
    cited = {citation.rsplit("coordination-hooks/", 1)[-1] for citation in raw_cited}
    res.check(bool(cited), "Verification section cites at least one test file")
    for relative in sorted(cited):
        res.check((PLUGIN_ROOT / relative).is_file(), f"cited test exists: {relative}")

    on_disk = {f"tests/{path.name}" for path in TESTS_DIR.iterdir() if path.is_file() and path.name.endswith("_smoke.py")}
    uncited = on_disk - cited
    res.check(not uncited, "every smoke in tests/ is cited in SECURITY.md", f"uncited: {sorted(uncited)}")


def check_artifact_is_self_contained(res: Results) -> None:
    """No test may reach outside the plugin, or the artifact stops standing alone.

    This is what the pre-existing repo-side driver could NOT satisfy: it resolves
    ``parents[3]`` to reach ``.claude/hooks/tests/``, which does not exist once
    the plugin directory is handed to a reviewer on its own.
    """
    escapes = ("parents[2]", "parents[3]", "parents[4]", "/.claude/", "os.pardir")
    for path in sorted(TESTS_DIR.glob("*.py")):
        # This file DEFINES the escape patterns, so its own source necessarily
        # contains them; scanning it would be a guaranteed self-hit.
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        for token in escapes:
            res.check(
                token not in source,
                f"{path.name} does not escape the plugin root",
                f"found {token!r}",
            )


def check_plugin_manifest_loads(res: Results) -> None:
    """The manifest that MOUNTS every hook must itself be loadable.

    Claude Code validates `.claude-plugin/plugin.json` as a whole: one
    malformed optional field rejects the ENTIRE manifest and registers zero
    hooks. Every other check in this file verifies hook scripts that, in that
    state, never run at all. Measured
    2026-07-31: `"homepage": "<<PLUGIN_HOMEPAGE_URL>>"` failed URL validation
    and Claude Code reported "Registered 0 hooks from 1 plugins".

    So: no unsubstituted `<<…>>` placeholder may survive anywhere in the
    manifest, and `homepage`, if present, must be a real absolute URL. This
    is the manifest-level member of the same family as the gate's routing
    bug — the thing that carries input to the checked code is as much part
    of the artifact as the code.
    """
    relative = ".claude-plugin/plugin.json"
    path = PLUGIN_ROOT / relative
    if not res.check(path.is_file(), f"plugin manifest exists: {relative}"):
        return
    raw = path.read_text(encoding="utf-8")

    placeholders = sorted(set(re.findall(r"<<[^<>]+>>", raw)))
    res.check(
        not placeholders,
        f"no unsubstituted placeholder survives in {relative}",
        f"found {placeholders} — no substitution machinery exists for the `<<…>>` form, so these ship verbatim",
    )
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        res.check(False, f"{relative} is valid JSON", str(exc))
        return

    homepage = manifest.get("homepage")
    if homepage is not None:
        res.check(
            isinstance(homepage, str) and re.match(r"^https?://[^\s<>]+\.[^\s<>]+", homepage) is not None,
            "manifest 'homepage' is an absolute http(s) URL",
            f"got {homepage!r} — this field alone rejects the whole manifest",
        )
    author = manifest.get("author")
    if author is not None:
        email = author.get("email") if isinstance(author, dict) else None
        if email is not None:
            res.check(
                isinstance(email, str) and re.match(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$", email) is not None,
                "manifest author.email is a real address",
                f"got {email!r}",
            )


# The hooks.json groups that existed before recovery_claude.py was registered (0.9.3, base commit
# 2093d1ac9), pinned by digest so "no existing entry changed" is a measurement rather than a
# reading. Each digest is the SHA-256 of the group's canonical JSON (sorted keys, no spaces); the
# comment names the group. An existing entry may change only together with SECURITY.md, and then
# the new digest goes here in the same patch.
PINNED_EXISTING: dict[str, tuple[str, ...]] = {
    "UserPromptSubmit": ("2764eeb103877a0d112893b2ee4653451cfaaa0f373f3a223c5d9237f788601f",),  # session_context.py
    "SessionStart": (
        "1820ff5358bfbbcb458d7319573d297ee7180026efe1abc72999f434e2094e01",  # startup|resume|clear|compact: step_zero_reminder.py, session_start_context.py (timeout 8 to 30, was 280dbb2fb996ffb90be783b3c0f085529c5e2c01c0685228f4dafa202be9a27d)
        "0890d327eec45297b428eb4b4092e325f80ac859c89004a2015bfba43f194684",  # session_context.py
        "4506c07f1f8b4bb9651e759fec6368423f2b1544a5511eeb9519674d5fac5a7f",  # capture_session_mapping.py
    ),
    "Stop": ("8b5d3b9a1d91e60d4f4f8c7f5d04d71b68ff0e63d75145aa28d35083dbb4db50",),  # wake_waiter.py (asyncRewake)
    "PreToolUse": ("79f3f9dccf381478b4c314c365077a67a5c29a036bd1c0b4263bacecbc2ca7dc",),  # headless_tool_allowlist_gate.py (direct route)
    "PostToolUse": (
        "e64760fb7ab72ff02e0b09d67e400382e77168ef145c53c726d75ab2b949ea39",  # rotation_due_watch.py
        "c133c772ff2fc9689cbba2ff44977ee7625084707bb52834cc2de7c1afdb79e8",  # Write|Edit|MultiEdit: capture.py
        "6f23b0ca5883bcdd9cf0a1cecc35d3eb65e63754a250c3298636f4e6e8abc46f",  # CronCreate|ScheduleWakeup: session_cron_sentinel.py
    ),
}

# What the recovery registration must be, per event: (matcher, timeout seconds, async). Every entry is
# exec form through the optional runner, so a script missing from a stale or partial plugin cache is
# skipped with exit 0; a bare interpreter call on a missing file exits 2, which blocks a PreToolUse.
RECOVERY_REGISTRATION: dict[str, tuple[str, int, bool]] = {
    "PreToolUse": ("Bash", 15, False),
    "PostToolUse": ("Write|Edit|MultiEdit|NotebookEdit|Bash", 10, False),
    "Stop": ("", 120, True),
    "SessionEnd": ("", 10, False),
    "SessionStart": ("startup|resume|clear", 10, False),
}
_RUNNER_TOKEN = PLUGIN_ROOT_TOKEN + OPTIONAL_RUNNER
_ADAPTER_TOKEN = PLUGIN_ROOT_TOKEN + RECOVERY_ADAPTER


def _group_digest(group: object) -> str:
    return hashlib.sha256(json.dumps(group, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _recovery_group_problems(event: str, group: object, matcher: str, timeout: int, is_async: bool) -> list[str]:
    if not isinstance(group, dict) or not set(group) <= {"matcher", "hooks"}:
        return [f"{event}: the recovery group is not a plain matcher group"]
    problems: list[str] = []
    if group.get("matcher", "") != matcher:
        problems.append(f"{event}: matcher is {group.get('matcher', '')!r}, expected {matcher!r}")
    inner = group.get("hooks")
    if not isinstance(inner, list) or len(inner) != 1 or not isinstance(inner[0], dict):
        return [*problems, f"{event}: the recovery group must hold exactly one hook"]
    return [*problems, *_recovery_hook_problems(event, inner[0], timeout, is_async)]


def _recovery_hook_problems(event: str, hook: dict[str, Any], timeout: int, is_async: bool) -> list[str]:
    expected_keys = {"type", "command", "args", "timeout"} | ({"async"} if is_async else set())
    checks = (
        (set(hook) == expected_keys, f"hook keys are {sorted(hook)}, expected {sorted(expected_keys)}"),
        (hook.get("type") == "command" and hook.get("command") == "python3", "not exec form with the bare python3 interpreter"),
        (hook.get("args") == [_RUNNER_TOKEN, _ADAPTER_TOKEN, event], f"args are {hook.get('args')!r}, expected the optional runner, the adapter and {event!r}"),
        (hook.get("timeout") == timeout, f"timeout is {hook.get('timeout')!r}, expected {timeout}"),
        (not is_async or hook.get("async") is True, "async must be true"),
    )
    return [f"{event}: {message}" for sound, message in checks if not sound]


def _pinned_problems(hooks: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for event, pinned in PINNED_EXISTING.items():
        groups = hooks.get(event)
        if not isinstance(groups, list) or len(groups) < len(pinned):
            problems.append(f"{event}: fewer groups than the {len(pinned)} existing ones")
            continue
        problems.extend(f"{event}[{index}]: an existing entry changed" for index, expected in enumerate(pinned) if _group_digest(groups[index]) != expected)
    return problems


def _added_problems(event: str, groups: object) -> list[str]:
    extra = groups[len(PINNED_EXISTING.get(event, ())) :] if isinstance(groups, list) else []
    wanted = RECOVERY_REGISTRATION.get(event)
    if wanted is None:
        return [f"{event}: unexpected new group"] * len(extra)
    if len(extra) != 1:
        return [f"{event}: expected exactly one recovery group, found {len(extra)}"]
    return _recovery_group_problems(event, extra[0], *wanted)


def _registration_problems(manifest: dict[str, Any]) -> list[str]:
    """Every way a hooks.json departs from 'the pinned existing groups, then exactly the five recovery groups'."""
    hooks = manifest.get("hooks")
    if not isinstance(hooks, dict):
        return ["hooks.json has no hooks object"]
    problems = _pinned_problems(hooks)
    for event, groups in hooks.items():
        problems.extend(_added_problems(event, groups))
    problems.extend(f"{event}: no recovery registration" for event in RECOVERY_REGISTRATION if event not in hooks)
    return problems


def _recovery_hook(manifest: dict[str, Any], event: str) -> dict[str, Any]:
    hook = manifest["hooks"][event][-1]["hooks"][0]
    assert isinstance(hook, dict)
    return hook


# Named defects. Each is applied to a copy of the real hooks.json and the registration check must
# turn red for it, so the check is proven able to fail on the very properties SECURITY.md claims.
REGISTRATION_MUTATIONS: dict[str, Callable[[dict[str, Any]], object]] = {
    "Stop recovery entry loses async": lambda m: _recovery_hook(m, "Stop").pop("async"),
    "Stop recovery entry becomes asyncRewake": lambda m: (_recovery_hook(m, "Stop").pop("async"), _recovery_hook(m, "Stop").update({"asyncRewake": True})),
    "PreToolUse recovery entry loses its timeout": lambda m: _recovery_hook(m, "PreToolUse").pop("timeout"),
    "Stop timeout raised to a day": lambda m: _recovery_hook(m, "Stop").update({"timeout": 86400}),
    "PreToolUse recovery entry bypasses the optional runner": lambda m: _recovery_hook(m, "PreToolUse").update({"args": [_ADAPTER_TOKEN, "PreToolUse"]}),
    "PreToolUse recovery entry becomes a shell string": lambda m: _recovery_hook(m, "PreToolUse").update({"command": "sh", "args": ["-c", "python3 recovery_claude.py"]}),
    "PreToolUse matcher widened to every tool": lambda m: m["hooks"]["PreToolUse"][-1].pop("matcher"),
    "SessionEnd recovery entry removed": lambda m: m["hooks"].pop("SessionEnd"),
    "a second recovery entry added to Stop": lambda m: m["hooks"]["Stop"].append(copy.deepcopy(m["hooks"]["Stop"][-1])),
    "an output-steering key added to a recovery entry": lambda m: _recovery_hook(m, "PostToolUse").update({"decision": "block"}),
    "an existing entry's timeout changed": lambda m: m["hooks"]["SessionStart"][0]["hooks"][1].update({"timeout": 31}),
    "an existing entry removed": lambda m: m["hooks"]["UserPromptSubmit"].pop(0),
}


def check_recovery_registration(res: Results, manifest: dict[str, Any]) -> None:
    """hooks.json = the pinned existing entries unchanged, plus exactly the five recovery entries."""
    problems = _registration_problems(manifest)
    res.check(not problems, "hooks.json keeps every existing entry unchanged and adds exactly the five recovery entries", "; ".join(problems))
    for name, mutate in REGISTRATION_MUTATIONS.items():
        mutated = copy.deepcopy(manifest)
        try:
            mutate(mutated)
        except (KeyError, IndexError, AttributeError) as exc:
            res.fail(f"mutation could not be applied: {name}", repr(exc))
            continue
        res.check(bool(_registration_problems(mutated)), f"mutation is caught: {name}", "the registration check still passes with this defect")


def _inventory_is_sound(engine: list[str]) -> bool:
    return bool(engine) and len(engine) == len(set(engine)) and all(name.endswith(".py") and Path(name).name == name for name in engine)


def _engine_sources(res: Results, engine: list[str]) -> dict[str, str]:
    """Check the shipped MODULES inventory against the directory and return the source of each present module."""
    res.check(_inventory_is_sound(engine), "the shipped MODULES inventory is a non-empty list of unique module files", f"got {engine}")
    res.check(ENGINE_FACADE in engine and ENGINE_SPAWN_BOUNDARY in engine, "the inventory names the facade and the spawn boundary")
    present = [name for name in engine if (HOOKS_DIR / name).is_file()]
    res.check(present == engine, "every module the inventory names is shipped beside the hooks", f"missing {sorted(set(engine) - set(present))}")
    return {name: (HOOKS_DIR / name).read_text(encoding="utf-8") for name in present}


def _check_engine_spawning(res: Results, sources: dict[str, str]) -> None:
    spawners = sorted(name for name, source in sources.items() if any(token in source for token in SUBPROCESS_PRIMITIVES))
    res.check(spawners == [ENGINE_SPAWN_BOUNDARY], "exactly one engine module can spawn a child", f"found {spawners}")
    shells = sorted(name for name, source in sources.items() if re.search(r"\bshell\s*=\s*True", source) or "os.system" in source or "os.popen" in source)
    res.check(not shells, "no engine module hands a command to a shell", f"found {shells}")


def _check_engine_git_subcommands(res: Results, sources: dict[str, str]) -> None:
    """SECURITY.md: a save writes nothing to the user's repository.

    The git subcommands that could (history, refs, packs, the network) are named nowhere in the save
    path, and the ones that write objects or the index appear only in the restore modules, which run
    on an explicit `restore`.
    """
    scanned = {name: _string_constants(source, name) for name, source in sources.items() if name.startswith("recovery_") or name in {"custody_store.py", ENGINE_FACADE}}
    named = sorted((name, word) for name, words in scanned.items() for word in words & ENGINE_ABSENT_SUBCOMMANDS)
    res.check(not named, "no recovery module names a git subcommand that writes history, refs or packs, or reaches a remote", f"found {named}")
    writers = sorted(name for name, words in scanned.items() if words & ENGINE_RESTORE_ONLY_SUBCOMMANDS and name not in ENGINE_RESTORE_MODULES)
    res.check(not writers, "git subcommands that write objects or the index appear only in the restore modules", f"found {writers}")


def _check_adapter_imports(res: Results) -> None:
    adapter = HOOKS_DIR / RECOVERY_ADAPTER
    if res.check(adapter.is_file(), f"{RECOVERY_ADAPTER} exists"):
        extra = sorted(_py_imports(adapter.read_text(encoding="utf-8"), RECOVERY_ADAPTER) - ADAPTER_IMPORTS)
        res.check(not extra, f"{RECOVERY_ADAPTER} imports only the stdlib it needs and the engine facade", f"also imports {extra}")


def check_engine_contract(res: Results, engine: list[str]) -> None:
    """The shipped engine is the listed set, one spawn boundary, no shell, no network; the adapter stays thin."""
    sources = _engine_sources(res, engine)
    _check_engine_spawning(res, sources)
    _check_engine_git_subcommands(res, sources)
    _check_adapter_imports(res)


def check_engine_documented(res: Results, engine: list[str]) -> None:
    """README.md and SECURITY.md name the adapter, every engine module and each registration bound."""
    for surface in ("README.md", "SECURITY.md"):
        text = _read_prose(surface)
        missing = [name for name in (RECOVERY_ADAPTER, *engine) if name not in text]
        res.check(not missing, f"{surface} names the recovery adapter and every engine module", f"missing {missing}")
    security = _read_prose("SECURITY.md")
    for event, (matcher, timeout, is_async) in RECOVERY_REGISTRATION.items():
        res.check(event in security and (not matcher or matcher in security) and f"timeout {timeout}" in security, f"SECURITY.md states the {event} recovery registration", f"needs {event}, {matcher!r} and 'timeout {timeout}'")
        if is_async:
            res.check("async" in security, f"SECURITY.md states that the {event} recovery entry is async")


def main() -> int:
    preflight()
    res = Results("coordination-hooks — manifest and security-claim consistency")

    engine = list(_engine_modules())
    hooks = _entry_point_hooks()
    siblings = _sibling_modules()
    manifest = json.loads(_read("hooks/hooks.json"))
    entries = _manifest_entries(manifest)

    print(f"Entry-point hooks: {len(hooks)} — {', '.join(hooks)}")
    print(f"Sibling modules:   {len(siblings)} — {', '.join(siblings) or 'none'}")
    print(f"Engine modules:    {len(engine)} (the shipped MODULES inventory; not hooks)")
    print(f"Manifest entries:  {len(entries)}")
    print("-" * 68)

    res.check(bool(hooks), "at least one hook exists", "hooks/ has no entry points")
    res.check(bool(entries), "hooks.json registers at least one hook")

    check_cron_matchers(res, manifest)
    check_manifest_is_exec_form(res, entries)
    check_optional_runner_contract(res, entries)
    check_reminder_cadence(res, manifest)
    check_session_start_sources(res, manifest)
    check_heartbeat_hook_is_deleted(res, entries)
    check_tree_matches_manifest(res, entries, hooks)
    check_recovery_registration(res, manifest)
    check_engine_contract(res, engine)
    check_siblings_are_not_orphans(res, siblings, hooks)
    check_every_hook_is_documented(res, hooks)
    check_engine_documented(res, engine)
    check_security_md_counts(res, hooks)
    check_no_network(res, hooks, [*siblings, OPTIONAL_RUNNER, *engine])
    # Engine modules are deliberately not scanned here: writing the local recovery store is their
    # disclosed purpose (SECURITY.md, "Working-tree recovery"), and recovery_claude.py itself writes no file.
    check_no_file_writes(res, hooks, [*siblings, OPTIONAL_RUNNER])
    check_file_write_hooks_write_only_markers(res)
    check_subprocess_capable_hooks(res, hooks, [*siblings, OPTIONAL_RUNNER])
    check_child_output_is_unread(res)
    check_docs_name_the_bounded_wake_argv(res)
    check_stdlib_only(res, hooks, [*siblings, OPTIONAL_RUNNER, *engine])
    check_verification_section(res)
    check_artifact_is_self_contained(res)
    check_plugin_manifest_loads(res)

    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
