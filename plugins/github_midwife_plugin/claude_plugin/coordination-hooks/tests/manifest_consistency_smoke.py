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
import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from _harness import (  # noqa: E402
    HOOKS_DIR,
    PLUGIN_ROOT,
    TESTS_DIR,
    Results,
    is_stdlib_module,
    node_builtin_modules,
    preflight,
)

# Each entry-point hook and the words a prose surface may use to name it. A new
# hook must be added here, and then every document below must mention it -- that
# coupling is the whole point of this file.
HOOK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "step_zero_reminder.js": ("knowledge-base-first", "kb-first", "knowledge base"),
    "check_messages_reminder.js": ("check-your-messages", "check your messages"),
    "role_binding_reminder.js": ("role-binding", "role binding"),
    "wake_waiter.js": ("wake waiter", "idle-wake", "wake_waiter"),
    "git_controller_gate.py": ("git-mutation", "git-controller", "git controller"),
}

# Documents that must name every hook. hooks.json and plugin.json carry a
# one-line description; README.md and SECURITY.md carry the prose.
PROSE_SURFACES = ("hooks/hooks.json", ".claude-plugin/plugin.json", "README.md", "SECURITY.md")

# Module names that would falsify "no network I/O of any kind in any hook".
NETWORK_MODULES = frozenset(
    {
        "http", "https", "http2", "net", "dgram", "tls", "dns", "socket",
        "urllib", "urllib2", "urllib3", "httplib", "http.client", "ftplib",
        "telnetlib", "smtplib", "requests", "httpx", "aiohttp", "websocket",
        "websockets", "node:http", "node:https", "node:net", "node:dgram",
        "node:tls", "node:dns", "node:http2",
    }
)

# Global network entry points that need no import at all.
NETWORK_GLOBALS = ("fetch(", "XMLHttpRequest", "navigator.sendBeacon", "new WebSocket")

# Source-level names that would falsify "No hook writes a file as an action of
# its own." Reads are fine and expected (the gate reads the session file).
WRITE_PRIMITIVES = (
    "writeFileSync", "writeFile(", "appendFile", "createWriteStream", "mkdirSync",
    "unlinkSync", "rmSync", "renameSync", "copyFileSync",
    "write_text", "write_bytes", "os.remove", "os.unlink", "os.mkdir", "os.makedirs",
    "shutil.", "tempfile.NamedTemporary", "os.rename",
)

# Source-level names that would falsify "Exactly one subprocess execution exists
# in the plugin." Checked across every hook; only the wake waiter may match.
SUBPROCESS_PRIMITIVES = (
    "child_process", "spawnSync", "spawn(", "execSync", "execFile", "execFileSync",
    "subprocess", "os.system", "os.popen", "os.execv", "os.execl", "os.spawn", "os.fork",
    "pty.spawn", "commands.getoutput",
)
SUBPROCESS_OWNER = "wake_waiter.js"

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}

# Shell metacharacters that must never appear in an exec-form argv. `${VAR}` is
# Claude Code's own manifest substitution and is expressly allowed; `$(` is not.
SHELL_METACHARACTERS = ("$(", "`", ";", "|", "&", ">", "<", "\n")

ALLOWED_COMMANDS = frozenset({"node", "python3"})
PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}/hooks/"


def _read(relative: str) -> str:
    return (PLUGIN_ROOT / relative).read_text(encoding="utf-8")


def _read_prose(relative: str) -> str:
    """Read a document with newlines collapsed, so hard-wrapped phrases still match."""
    return re.sub(r"\s+", " ", _read(relative))


def _entry_point_hooks() -> list[str]:
    """Hook scripts Claude Code invokes directly (siblings start with '_')."""
    return sorted(
        path.name
        for path in HOOKS_DIR.iterdir()
        if path.is_file() and path.suffix in {".js", ".py"} and not path.name.startswith("_")
    )


def _sibling_modules() -> list[str]:
    return sorted(
        path.name
        for path in HOOKS_DIR.iterdir()
        if path.is_file() and path.suffix == ".py" and path.name.startswith("_")
    )


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


def _js_requires(source: str) -> set[str]:
    return set(re.findall(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", source))


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


def check_tree_matches_manifest(res: Results, entries: list[dict[str, object]], hooks: list[str]) -> None:
    referenced: set[str] = set()
    for entry in entries:
        args = entry.get("args")
        if isinstance(args, list) and args and isinstance(args[0], str):
            referenced.add(Path(args[0]).name)

    for name in referenced:
        res.check((HOOKS_DIR / name).is_file(), f"hooks.json path exists on disk: {name}")

    for name in hooks:
        res.check(name in referenced, f"hook is wired in hooks.json: {name}", "present on disk but never invoked")

    orphans = referenced - set(hooks)
    res.check(not orphans, "no hooks.json entry points at a missing script", f"dangling: {sorted(orphans)}")

    res.check(
        set(hooks) == set(HOOK_KEYWORDS),
        "HOOK_KEYWORDS covers exactly the hooks on disk",
        f"on disk {sorted(set(hooks) - set(HOOK_KEYWORDS))}, "
        f"in table only {sorted(set(HOOK_KEYWORDS) - set(hooks))}",
    )


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
    reminders = len([name for name in hooks if name.endswith("_reminder.js")])

    res.check(
        f"is {_NUMBER_WORDS[total]} Claude Code hooks" in text,
        "SECURITY.md hook count matches the tree",
        f"tree has {total}; expected the phrase 'is {_NUMBER_WORDS[total]} Claude Code hooks'",
    )
    res.check(
        f"{_NUMBER_WORDS[reminders]} context reminders" in text,
        "SECURITY.md reminder count matches the tree",
        f"tree has {reminders} *_reminder.js",
    )
    res.check(
        f"other {_NUMBER_WORDS[total - 1]} hooks execute nothing" in text,
        "SECURITY.md 'execute nothing' count matches the tree",
        f"expected 'other {_NUMBER_WORDS[total - 1]} hooks execute nothing'",
    )


def check_no_network(res: Results, hooks: list[str], siblings: list[str]) -> None:
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        modules = _js_requires(source) if name.endswith(".js") else _py_imports(source, name)
        offending = sorted(modules & NETWORK_MODULES)
        res.check(not offending, f"no network module in {name}", f"imports {offending}")
        if name.endswith(".js"):
            hits = [token for token in NETWORK_GLOBALS if token in source]
            res.check(not hits, f"no global network call in {name}", f"found {hits}")


def check_no_file_writes(res: Results, hooks: list[str], siblings: list[str]) -> None:
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        hits = [token for token in WRITE_PRIMITIVES if token in source]
        res.check(not hits, f"no file-write primitive in {name}", f"found {hits}")


def check_exactly_one_subprocess(res: Results, hooks: list[str], siblings: list[str]) -> None:
    owners: list[str] = []
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        if any(token in source for token in SUBPROCESS_PRIMITIVES):
            owners.append(name)
    res.check(
        owners == [SUBPROCESS_OWNER],
        "exactly one hook can execute a subprocess",
        f"expected only {SUBPROCESS_OWNER}, found {owners}",
    )

    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    # `shell: true` is the option that would turn a fixed argv into a shell
    # string. Match the option form, not the word -- the source says "no shell"
    # in prose, and a comment must not be able to fail this check.
    res.check(
        re.search(r"\bshell\s*:", waiter) is None,
        f"{SUBPROCESS_OWNER} never passes a shell option",
        "a `shell:` option appears in the source",
    )
    res.check(
        waiter.count("spawnSync") >= 1 and not re.search(r"\bexec(Sync|File|FileSync)?\s*\(", waiter),
        f"{SUBPROCESS_OWNER} uses spawnSync only",
        "an exec-family call is present",
    )
    res.check(
        'spawnSync(cli, ["wake"]' in waiter,
        f"{SUBPROCESS_OWNER} argv is fixed",
        "expected a literal [\"wake\"] argument vector",
    )


def check_child_output_is_unread(res: Results) -> None:
    """SECURITY.md: the child's stdout/stderr are 'dropped unread'.

    This has to be a SOURCE check, not a behavioural one. Switching the spawn to
    `stdio: "pipe"` reads the child's streams into the parent -- falsifying
    "unread" -- while still emitting nothing, so no black-box test can see it.
    Mutation testing found exactly this hole.
    """
    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    res.check(
        re.search(r'stdio\s*:\s*\[\s*"ignore"\s*,\s*"ignore"\s*,\s*"ignore"\s*\]', waiter) is not None,
        f"{SUBPROCESS_OWNER} spawns with all three streams ignored",
        "the child's streams are not all set to 'ignore'",
    )
    for stream in ("result.stdout", "result.stderr", "result.output"):
        res.check(
            stream not in waiter,
            f"{SUBPROCESS_OWNER} never reads {stream}",
            "the child's output is referenced in the source",
        )


def check_stdlib_only(res: Results, hooks: list[str], siblings: list[str], builtins: frozenset[str]) -> None:
    sibling_modules = {Path(name).stem for name in siblings}
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        if name.endswith(".js"):
            for module in sorted(_js_requires(source)):
                res.check(
                    module in builtins or module.removeprefix("node:") in builtins,
                    f"{name} requires only Node built-ins",
                    f"{module!r} is not a built-in module",
                )
        else:
            for module in sorted(_py_imports(source, name)):
                res.check(
                    is_stdlib_module(module) or module in sibling_modules,
                    f"{name} imports only the Python stdlib",
                    f"{module!r} is neither stdlib nor a sibling module",
                )


def check_verification_section(res: Results) -> None:
    """SECURITY.md's Verification section must cite tests that actually exist."""
    text = _read("SECURITY.md")
    if not res.check("## Verification" in text, "SECURITY.md has a Verification section"):
        return
    cited = set(re.findall(r"`(tests/[A-Za-z0-9_]+\.py)`", text))
    res.check(bool(cited), "Verification section cites at least one test file")
    for relative in sorted(cited):
        res.check((PLUGIN_ROOT / relative).is_file(), f"cited test exists: {relative}")

    on_disk = {
        f"tests/{path.name}"
        for path in TESTS_DIR.iterdir()
        if path.is_file() and path.name.endswith("_smoke.py")
    }
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


def main() -> int:
    preflight()
    res = Results("coordination-hooks — manifest and security-claim consistency")

    hooks = _entry_point_hooks()
    siblings = _sibling_modules()
    manifest = json.loads(_read("hooks/hooks.json"))
    entries = _manifest_entries(manifest)
    builtins = node_builtin_modules()

    print(f"Entry-point hooks: {len(hooks)} — {', '.join(hooks)}")
    print(f"Sibling modules:   {len(siblings)} — {', '.join(siblings) or 'none'}")
    print(f"Manifest entries:  {len(entries)}")
    print("-" * 68)

    res.check(bool(hooks), "at least one hook exists", "hooks/ has no entry points")
    res.check(bool(entries), "hooks.json registers at least one hook")

    check_manifest_is_exec_form(res, entries)
    check_tree_matches_manifest(res, entries, hooks)
    check_siblings_are_not_orphans(res, siblings, hooks)
    check_every_hook_is_documented(res, hooks)
    check_security_md_counts(res, hooks)
    check_no_network(res, hooks, siblings)
    check_no_file_writes(res, hooks, siblings)
    check_exactly_one_subprocess(res, hooks, siblings)
    check_child_output_is_unread(res)
    check_stdlib_only(res, hooks, siblings, builtins)
    check_verification_section(res)
    check_artifact_is_self_contained(res)

    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
