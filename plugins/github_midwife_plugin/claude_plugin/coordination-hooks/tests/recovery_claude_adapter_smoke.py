#!/usr/bin/env python3
"""Drive hooks/recovery_claude.py through its real entry point.

The adapter is what Claude Code runs: JSON on stdin, the event name as the one
argument, an exit code and stdout back. Every case here is a real process with a
controlled environment; nothing imports the adapter or reaches a private helper.

Fixtures are SYNTHETIC (decision dec_23f279f7): they follow the documented Claude
Code hook payload shapes (common fields plus `tool_name`/`tool_input`/`tool_use_id`,
`reason`, `source`) and were not captured from a live session. The live acceptance
run of each tool is the seat's, in a disposable HOME. Two design risks therefore
stay UNMEASURED here and are documented, with their fallbacks, in README.md:
R3 (Claude Code honouring `hookSpecificOutput.additionalContext` on PreToolUse;
the fallback is `systemMessage` alone) and R6 (the SessionEnd time bound).

What is proved:
  * every event fixture (PreToolUse Bash; PostToolUse Write, Edit, MultiEdit,
    NotebookEdit and Bash; Stop; SessionEnd; SessionStart) reaches the engine and
    produces the documented effect, and the shipped modules restore what a
    discarding command removed;
  * stdout is empty or one JSON object whose keys all come from a closed set, at
    every depth, for every fixture including the fault fixtures; text that merely
    CONTAINS the words "decision" or "permissionDecision" stays a string value;
  * every fault fixture exits 0;
  * below Python 3.11 the adapter exits 0 before importing any engine module.

Run directly; exit 0 on success, non-zero on failure.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from _harness import HOOKS_DIR, PLUGIN_ROOT, Results, base_env, preflight  # noqa: E402

ADAPTER = HOOKS_DIR / "recovery_claude.py"
RUNNER = HOOKS_DIR / "optional_hook_runner.py"
ENGINE_CLI = HOOKS_DIR / "working_tree_recovery.py"
EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SessionEnd", "SessionStart")

ALLOWED_TOP = frozenset({"systemMessage", "hookSpecificOutput"})
ALLOWED_SPECIFIC = frozenset({"hookEventName", "additionalContext"})
# A key that could steer or stop the tool. None may appear at any depth.
FORBIDDEN_KEYS = frozenset({"decision", "permissionDecision", "permissionDecisionReason", "continue", "stopReason", "suppressOutput", "updatedInput", "reason"})
MIN_ENGINE_PYTHON = (3, 11)


def _engine_python() -> str | None:
    """An interpreter that can run the engine: this one, else the newest on PATH."""
    if sys.version_info >= MIN_ENGINE_PYTHON:
        return sys.executable
    for name in ("python3.14", "python3.13", "python3.12", "python3.11"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _git_dir_digest(repo: Path) -> str:
    """One digest over every file under .git (relative path, mode and bytes): a save must leave it unchanged."""
    digest = hashlib.sha256()
    for path in sorted((repo / ".git").rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest.update(f"{path.relative_to(repo)}\0{path.stat().st_mode:o}\0".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _registered() -> dict[str, list[list[str]]]:
    """The recovery command lines hooks.json registers, by event, exactly as Claude Code would run them."""
    manifest = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    found: dict[str, list[list[str]]] = {}
    for event, groups in manifest["hooks"].items():
        for group in groups:
            for entry in group["hooks"]:
                args = [str(arg).replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT)) for arg in entry.get("args", [])]
                if any(Path(arg).name == ADAPTER.name for arg in args):
                    found.setdefault(event, []).append(args)
    return found


def _keys(value: object) -> list[str]:
    """Every dict key at any depth."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_keys(item))
    return found


class Box:
    """A disposable HOME, recovery store and set of repositories."""

    def __init__(self, base: Path, python: str) -> None:
        self.base, self.python = base, python
        self.home = base / "home"
        self.root = base / "store"
        self.home.mkdir()
        self.env = {**base_env(), "HOME": str(self.home), "SOLET_RECOVERY_ROOT": str(self.root), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(self, repo: Path, *args: str) -> str:
        done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=repo, env=self.env, capture_output=True, text=True, check=True)
        return done.stdout

    def repo(self, name: str, files: dict[str, str]) -> Path:
        path = self.base / "work" / name
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        for relative, text in files.items():
            (path / relative).write_text(text, encoding="utf-8")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "base")
        return path

    def run(self, event: str, stdin: bytes, *, env: dict[str, str] | None = None, direct: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        """Run the command hooks.json registers for the event, or the script itself with its own argument when direct."""
        registered = _registered().get(event, [])
        if direct or len(registered) != 1:
            argv = [self.python, "-B", str(ADAPTER), *([event] if event else [])]
        else:
            argv = [self.python, "-B", *registered[0]]
        return subprocess.run(argv, input=stdin, env={**self.env, **(env or {})}, cwd=cwd, capture_output=True, timeout=90, check=False)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.python, "-B", str(ENGINE_CLI), "--root", str(self.root), *args], env=self.env, capture_output=True, text=True, timeout=90, check=False)


def _payload(event: str, session: str, cwd: Path | str, **fields: Any) -> dict[str, Any]:
    """The common Claude Code hook fields plus the event's own."""
    return {"session_id": session, "transcript_path": "/nonexistent/transcript.jsonl", "cwd": str(cwd), "permission_mode": "default", "hook_event_name": event, **fields}


def _bash(session: str, cwd: Path | str, command: str, event: str = "PreToolUse") -> dict[str, Any]:
    extra: dict[str, Any] = {"tool_response": {"stdout": "", "stderr": "", "interrupted": False}} if event == "PostToolUse" else {}
    return _payload(event, session, cwd, tool_name="Bash", tool_input={"command": command, "description": "synthetic"}, tool_use_id="toolu_synthetic_01", **extra)


def _file_tool(session: str, cwd: Path, tool: str, path: Path) -> dict[str, Any]:
    """PostToolUse for a file tool, each with the field shape Claude Code gives it."""
    if tool == "Write":
        tool_input: dict[str, Any] = {"file_path": str(path), "content": "synthetic"}
    elif tool == "Edit":
        tool_input = {"file_path": str(path), "old_string": "a", "new_string": "b"}
    elif tool == "MultiEdit":
        tool_input = {"file_path": str(path), "edits": [{"old_string": "a", "new_string": "b"}]}
    else:
        tool_input = {"notebook_path": str(path), "new_source": "x", "edit_mode": "replace"}
    return _payload("PostToolUse", session, cwd, tool_name=tool, tool_input=tool_input, tool_response={"success": True}, tool_use_id=f"toolu_{tool.lower()}")


def _check_output(res: Results, label: str, done: subprocess.CompletedProcess[bytes], *, quiet: bool | None = None) -> dict[str, Any]:
    """Exit 0, stdout empty or one object of allowed keys only, stderr only [recovery] lines."""
    res.check(done.returncode == 0, f"{label}: exit 0", f"exit {done.returncode}: {done.stderr[-300:]!r}")
    text = done.stdout.decode("utf-8", "replace").strip()
    parsed: dict[str, Any] = {}
    if text:
        try:
            value = json.loads(text)
        except ValueError:
            res.fail(f"{label}: stdout is empty or one JSON object", f"got {text[:200]!r}")
            return parsed
        if not res.check(isinstance(value, dict), f"{label}: stdout is one JSON object", f"got {type(value).__name__}"):
            return parsed
        parsed = value
    res.check(set(parsed) <= ALLOWED_TOP, f"{label}: top-level keys are allowed", f"got {sorted(parsed)}")
    specific = parsed.get("hookSpecificOutput", {})
    res.check(isinstance(specific, dict) and set(specific) <= ALLOWED_SPECIFIC, f"{label}: hookSpecificOutput keys are allowed", f"got {specific!r}")
    found = sorted(set(_keys(parsed)) & FORBIDDEN_KEYS)
    res.check(not found, f"{label}: no decision-shaped key at any depth", f"found {found}")
    stray = [line for line in done.stderr.decode("utf-8", "replace").splitlines() if line.strip() and not line.startswith("[recovery]")]
    res.check(not stray, f"{label}: stderr carries only [recovery] lines", f"got {stray[:3]}")
    if quiet is not None:
        res.check(bool(parsed) != quiet, f"{label}: {'prints nothing' if quiet else 'prints a notice'}", f"stdout was {text[:200]!r}")
    return parsed


def case_pre_discard_saves_and_restores(res: Results, box: Box) -> None:
    """PreToolUse Bash: the save is made before the command; the shipped modules restore it."""
    repo = box.repo("pre", {"a.txt": "base\n", "permissionDecision.txt": "base\n", "decision.md": "base\n"})
    (repo / "a.txt").write_text("edited elsewhere\n", encoding="utf-8")
    (repo / "permissionDecision.txt").write_text("also edited\n", encoding="utf-8")
    before = _git_dir_digest(repo)
    done = box.run("PreToolUse", json.dumps(_bash("s-pre", repo, "git reset --hard")).encode())
    res.check(_git_dir_digest(repo) == before, "pre_discard: the save leaves the repository's .git byte-identical", "a file under .git changed")
    out = _check_output(res, "pre_discard", done, quiet=False)
    message = str(out.get("systemMessage", ""))
    context = str(out.get("hookSpecificOutput", {}).get("additionalContext", ""))
    res.check(out.get("hookSpecificOutput", {}).get("hookEventName") == "PreToolUse", "pre_discard: hookEventName is PreToolUse")
    res.check("a.txt" in message and "permissionDecision.txt" in message and "changed outside this session" in message, "pre_discard: the notice names what was saved", message[:300])
    res.check("permissionDecision.txt" in message and "permissionDecision" not in out, "pre_discard: the word permissionDecision stays inside a string value")
    res.check(context.startswith("Saved") and "tell them before continuing" in context, "pre_discard: additionalContext carries the model-facing text", context[:200])
    res.check((repo / "a.txt").read_text(encoding="utf-8") == "edited elsewhere\n", "pre_discard: the hook never runs or alters the command's target")
    box.git(repo, "reset", "--hard")  # the command Claude Code now runs
    res.check((repo / "a.txt").read_text(encoding="utf-8") == "base\n", "pre_discard: the discard really removed the edit")
    match = re.search(r"restore save ([0-9a-f]{8})", message)
    if not res.check(match is not None, "pre_discard: the notice names a restore command", message[-300:]):
        return
    assert match is not None
    restored = box.cli("restore", match.group(1))
    res.check(restored.returncode == 0, "pre_discard: the shipped restore verb succeeds", restored.stderr[-300:])
    back = [path for path in [repo / "a.txt", *box.root.rglob("a.txt")] if path.is_file() and path.read_text(encoding="utf-8") == "edited elsewhere\n"]
    res.check(bool(back), "pre_discard: the saved bytes are readable again after restore")


def case_own_edits_are_attributed(res: Results, box: Box) -> None:
    """PostToolUse fixtures for every file tool: own edits are not reported as foreign."""
    names = {"Write": "w.txt", "Edit": "e.txt", "MultiEdit": "m.txt", "NotebookEdit": "n.ipynb"}
    repo = box.repo("post", {**dict.fromkeys(names.values(), "base\n"), "x.txt": "base\n"})
    for name in [*names.values(), "x.txt"]:
        (repo / name).write_text("changed\n", encoding="utf-8")
    for tool, name in names.items():
        _check_output(res, f"post_{tool}", box.run("PostToolUse", json.dumps(_file_tool("s-post", repo, tool, repo / name)).encode()), quiet=True)
    _check_output(res, "post_Bash", box.run("PostToolUse", json.dumps(_bash("s-post", repo, "echo hi", "PostToolUse")).encode()), quiet=True)
    out = _check_output(res, "post_then_discard", box.run("PreToolUse", json.dumps(_bash("s-post", repo, "git checkout -- .")).encode()), quiet=False)
    message = str(out.get("systemMessage", ""))
    res.check("x.txt" in message, "attribution: the edit no tool reported is named as changed outside this session", message[:300])
    own = [name for name in names.values() if name in message]
    res.check(not own, "attribution: edits made through Write, Edit, MultiEdit and NotebookEdit are not named", f"named {own}")


def case_turn_boundaries(res: Results, box: Box) -> None:
    """Stop and SessionEnd save the working tree and print nothing; SessionStart is quiet when healthy."""
    repo = box.repo("turn", {"t.txt": "base\n"})
    (repo / "t.txt").write_text("unsaved\n", encoding="utf-8")
    stop = json.dumps(_payload("Stop", "s-turn", repo, stop_hook_active=False)).encode()

    def saved() -> list[dict[str, Any]]:
        listing = box.cli("list", "--folder", str(repo))
        return json.loads(listing.stdout) if listing.returncode == 0 and listing.stdout.strip() else []

    _check_output(res, "stop", box.run("Stop", stop), quiet=True)
    after_stop = saved()
    res.check([row.get("trigger") for row in after_stop] == ["turn_end"], "turns: Stop left one turn_end save", f"listing {after_stop}")
    (repo / "t.txt").write_text("unsaved, then changed again\n", encoding="utf-8")
    _check_output(res, "session_end", box.run("SessionEnd", json.dumps(_payload("SessionEnd", "s-turn", repo, reason="other")).encode()), quiet=True)
    after_end = saved()
    res.check(sorted(str(row.get("trigger")) for row in after_end) == ["session_end", "turn_end"], "turns: SessionEnd on a changed tree left its own save", f"listing {after_end}")
    _check_output(res, "stop_unchanged", box.run("Stop", stop), quiet=True)
    res.check(len(saved()) == len(after_end), "turns: a repeat save of an unchanged tree is stored once", f"listing {saved()}")
    _check_output(res, "session_start", box.run("SessionStart", json.dumps(_payload("SessionStart", "s-turn", repo, source="startup")).encode()), quiet=True)


def case_quiet_when_nothing_is_discarded(res: Results, box: Box) -> None:
    repo = box.repo("quiet", {"q.txt": "base\n"})
    (repo / "q.txt").write_text("edit\n", encoding="utf-8")
    _check_output(res, "non_discard", box.run("PreToolUse", json.dumps(_bash("s-q", repo, "ls -la")).encode()), quiet=True)
    _check_output(res, "non_bash_tool", box.run("PreToolUse", json.dumps(_payload("PreToolUse", "s-q", repo, tool_name="Read", tool_input={"file_path": str(repo / "q.txt")}, tool_use_id="toolu_r")).encode()), quiet=True)


def case_unwritable_store(res: Results, box: Box) -> None:
    """A store that cannot be written never blocks: the command runs and the person is told."""
    repo = box.repo("deny", {"d.txt": "base\n"})
    (repo / "d.txt").write_text("edit\n", encoding="utf-8")
    blocked = box.base / "not-a-directory"
    blocked.write_text("file", encoding="utf-8")
    env = {"SOLET_RECOVERY_ROOT": str(blocked / "store")}
    out = _check_output(res, "unwritable_pre", box.run("PreToolUse", json.dumps(_bash("s-d", repo, "git reset --hard")).encode(), env=env), quiet=False)
    res.check("the command will run" in str(out.get("systemMessage", "")), "unwritable_pre: the person is told the command will run", str(out)[:300])
    out = _check_output(res, "unwritable_start", box.run("SessionStart", json.dumps(_payload("SessionStart", "s-d", repo, source="resume")).encode(), env=env), quiet=False)
    specific = out.get("hookSpecificOutput", {})
    res.check(specific.get("hookEventName") == "SessionStart" and "Recovery is inactive" in str(specific.get("additionalContext", "")), "unwritable_start: SessionStart says recovery is inactive", str(out)[:300])
    res.check("systemMessage" not in out, "unwritable_start: SessionStart carries additionalContext only")


def case_fault_fixtures(res: Results, box: Box) -> None:
    """Every fault exits 0 with allowed output only."""
    repo = box.repo("fault", {"f.txt": "base\n"})
    (repo / "f.txt").write_text("edit\n", encoding="utf-8")
    plain = box.base / "plain"
    plain.mkdir()
    good = _bash("s-f", repo, "git reset --hard")
    without = {key: value for key, value in good.items() if key not in ("session_id",)}
    nowhere = {key: value for key, value in good.items() if key != "cwd"}
    cases: list[tuple[str, str, bytes]] = [
        ("empty stdin", "PreToolUse", b""),
        ("not json", "PreToolUse", b"{not json"),
        ("json array", "PreToolUse", b"[1, 2]"),
        ("json null", "PreToolUse", b"null"),
        ("binary stdin", "PreToolUse", bytes(range(256))),
        ("no event argument", "", json.dumps(good).encode()),
        ("unknown event", "PreCompact", json.dumps({**good, "hook_event_name": "PreCompact"}).encode()),
        ("argument and payload disagree", "PreToolUse", json.dumps({**good, "hook_event_name": "PostToolUse"}).encode()),
        ("missing session_id", "PreToolUse", json.dumps(without).encode()),
        ("missing cwd", "PreToolUse", json.dumps(nowhere).encode()),
        ("session_id is a number", "PreToolUse", json.dumps({**good, "session_id": 7}).encode()),
        ("cwd absent from disk", "PreToolUse", json.dumps({**good, "cwd": str(box.base / "gone")}).encode()),
        ("cwd is not a repository", "PreToolUse", json.dumps({**good, "cwd": str(plain)}).encode()),
        ("tool_input is a string", "PreToolUse", json.dumps({**good, "tool_input": "git reset --hard"}).encode()),
        ("command is a number", "PreToolUse", json.dumps({**good, "tool_input": {"command": 7}}).encode()),
        ("command is a list of numbers", "PreToolUse", json.dumps({**good, "tool_input": {"command": [1, 2]}}).encode()),
        ("deeply nested payload", "PostToolUse", json.dumps(_payload("PostToolUse", "s-f", repo, tool_name="Write", tool_input={"file_path": [[[[str(repo)]]]]})).encode()),
        ("oversized command", "PreToolUse", json.dumps({**good, "tool_input": {"command": "echo " + "x" * 200000 + "; git reset --hard"}}).encode()),
    ]
    # The runner swallows the event argument, so only a direct run can test how the script treats its own.
    direct_only = {"no event argument", "unknown event", "argument and payload disagree"}
    for label, event, stdin in cases:
        _check_output(res, f"fault[{label}]", box.run(event, stdin, direct=label in direct_only))
    process_cwd = box.base / "process-cwd"
    process_cwd.mkdir()
    done = box.run("PreToolUse", json.dumps(good).encode(), env={"SOLET_RECOVERY_ROOT": "", "HOME": str(box.base / "no-such-home")}, cwd=process_cwd)
    _check_output(res, "fault[no usable home or store root]", done)
    res.check(list(process_cwd.iterdir()) == [], "fault[no usable home or store root]: the hook's current folder stays empty", repr(list(process_cwd.iterdir())))


def _default_store(home: Path) -> Path:
    """Where the engine puts the store when no root is configured (XDG_DATA_HOME is not passed to the hook)."""
    return home / ("Library/Application Support" if sys.platform == "darwin" else ".local/share") / "Solet/Recovery/v1"


def case_empty_store_root_is_unset(res: Results, box: Box) -> None:
    """An empty SOLET_RECOVERY_ROOT means unset: the save lands in the default store, never in the hook's current folder."""
    repo = box.repo("emptyroot", {"e.txt": "base\n"})
    (repo / "e.txt").write_text("edit\n", encoding="utf-8")
    process_cwd = box.base / "empty-root-cwd"
    process_cwd.mkdir()
    done = box.run("PreToolUse", json.dumps(_bash("s-e", repo, "git reset --hard")).encode(), env={"SOLET_RECOVERY_ROOT": ""}, cwd=process_cwd)
    _check_output(res, "empty_root", done, quiet=False)
    res.check(list(process_cwd.iterdir()) == [], "empty_root: the hook's current folder stays empty", repr(list(process_cwd.iterdir())))
    saves = list((_default_store(box.home) / "packets").glob("*/*"))
    res.check(len(saves) == 1, "empty_root: the save is made in the default store under HOME", repr(saves))


def case_registration(res: Results) -> None:
    """Each event has exactly one recovery entry, run through the optional runner, with the declared bounds."""
    registered = _registered()
    for event in EVENTS:
        rows = registered.get(event, [])
        res.check(len(rows) == 1, f"registration[{event}]: exactly one recovery entry", f"found {len(rows)}")
        if len(rows) == 1:
            res.check(rows[0] == [str(RUNNER), str(ADAPTER), event], f"registration[{event}]: runs through optional_hook_runner.py with the event", f"got {rows[0]}")
    _check_declared_bounds(res)


def _check_declared_bounds(res: Results) -> None:
    manifest = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    entries = {event: entry for event, groups in manifest["hooks"].items() for group in groups for entry in group["hooks"] if ADAPTER.name in " ".join(entry.get("args", []))}
    stop = entries.get("Stop", {})
    res.check(stop.get("async") is True and stop.get("timeout") == 120, "registration[Stop]: async true with timeout 120")
    res.check({event: entries.get(event, {}).get("timeout") for event in ("PreToolUse", "PostToolUse", "SessionEnd", "SessionStart")} == {"PreToolUse": 15, "PostToolUse": 10, "SessionEnd": 10, "SessionStart": 10}, "registration: the declared timeouts")
    res.check(all("async" not in entry for event, entry in entries.items() if event != "Stop"), "registration: only Stop is async")


def case_missing_adapter_never_blocks(res: Results, box: Box) -> None:
    """A script missing from a stale or partial plugin cache is skipped with exit 0; a bare interpreter call would exit 2 and block PreToolUse."""
    gone = box.base / "stale-cache" / "hooks" / "recovery_claude.py"
    done = subprocess.run([box.python, "-B", str(RUNNER), str(gone), "PreToolUse"], input=b"{}", env=box.env, capture_output=True, timeout=60, check=False)
    res.check(done.returncode == 0 and done.stdout == b"", "missing script: the runner skips it with exit 0 and no output", f"exit {done.returncode} {done.stdout[:100]!r} {done.stderr[-200:]!r}")


def case_output_words_never_become_keys(res: Results) -> None:
    """The adapter source holds no decision-shaped key name; the closed key set is the only vocabulary."""
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
    constants = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    present = sorted(constants & FORBIDDEN_KEYS)
    res.check(not present, "adapter source: no string constant names a decision-shaped key", f"found {present}")
    res.check({"systemMessage", "hookSpecificOutput", "hookEventName", "additionalContext"} <= constants, "adapter source: emits the allowed keys")


def case_floor_below_3_11(res: Results, box: Box) -> None:
    """Below Python 3.11 the adapter exits 0 before importing the engine; SessionStart says why."""
    code = "import sys, runpy\nsys.version_info = (3, 9, 6, 'final', 0)\nsys.argv = [sys.argv[1], sys.argv[2]]\ncode = 'none'\ntry:\n    runpy.run_path(sys.argv[0], run_name='__main__')\nexcept SystemExit as stop:\n    code = stop.code\nprint('floor-probe imported=%s code=%s' % ('working_tree_recovery' in sys.modules, code), file=sys.stderr)\n"
    for event, expect_notice in (("SessionStart", True), ("PreToolUse", False), ("Stop", False)):
        stdin = json.dumps(_bash("s-floor", box.base, "git reset --hard") if event == "PreToolUse" else _payload(event, "s-floor", box.base, source="startup")).encode()
        done = subprocess.run([box.python, "-B", "-c", code, str(ADAPTER), event], input=stdin, env=box.env, capture_output=True, timeout=60, check=False)
        label = f"floor[{event}]"
        res.check(done.returncode == 0, f"{label}: exit 0", f"exit {done.returncode}: {done.stderr[-300:]!r}")
        probe = done.stderr.decode("utf-8", "replace")
        res.check("imported=False" in probe, f"{label}: no engine module was imported", probe[-300:])
        text = done.stdout.decode("utf-8", "replace").strip()
        if expect_notice:
            value = json.loads(text) if text else {}
            specific = value.get("hookSpecificOutput", {})
            res.check(set(value) == {"hookSpecificOutput"} and specific.get("hookEventName") == "SessionStart" and "3.11" in str(specific.get("additionalContext", "")), f"{label}: one advisory in the allowed shape", text[:300])
        else:
            res.check(text == "", f"{label}: prints nothing", text[:200])


def main() -> int:
    preflight()
    res = Results("coordination-hooks — recovery_claude.py adapter, real entry point")
    if not res.check(ADAPTER.is_file(), "hooks/recovery_claude.py exists", f"not found: {ADAPTER}"):
        return res.finish()
    res.check(ENGINE_CLI.is_file(), "hooks/working_tree_recovery.py (the materialized engine facade) exists", f"not found: {ENGINE_CLI}")
    res.check(shutil.which("git") is not None, "git is on PATH for the scenarios")
    python = _engine_python()
    if not res.check(python is not None, "a Python 3.11+ interpreter is available to run the engine", f"this one is {sys.version_info[0]}.{sys.version_info[1]} and none was found on PATH"):
        return res.finish()
    assert python is not None
    with tempfile.TemporaryDirectory(prefix="recovery-claude-adapter-") as temporary:
        box = Box(Path(temporary).resolve(), python)
        for case in (case_pre_discard_saves_and_restores, case_own_edits_are_attributed, case_turn_boundaries, case_quiet_when_nothing_is_discarded, case_unwritable_store, case_fault_fixtures, case_empty_store_root_is_unset, case_floor_below_3_11, case_missing_adapter_never_blocks):
            case(res, box)
        case_registration(res)
        case_output_words_never_become_keys(res)
    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
