#!/usr/bin/env python3
"""Drive the Codex recovery adapter through its real entry point and pin how it is registered.

The adapter runs as a subprocess exactly as the manifest declares it: hook JSON on
stdin, the exit code and stdout read back, a temporary HOME, a temporary store root
and a scratch git repository. Payload fixtures are SYNTHETIC (decision dec_23f279f7):
each is validated against the hook input schema that Codex 0.163.0 embeds in its binary,
copied below with the title Codex gives it, and every byte the adapter prints is
validated against the matching embedded output schema. The live Codex acceptance, which
alone can prove tool names, trust state and hook timeouts, is a separate leg.

Pinned here: Stop, PostToolUse and SessionEnd print nothing; PreToolUse and SessionStart
print only systemMessage and hookSpecificOutput.additionalContext and never a decision;
every fault fixture exits 0; the manifest registers each recovery entry with its declared
matcher, timeout and async flag while the four earlier handlers stay byte-identical; and
three named mutations (drop async, add output, add decision) each fail on their own
assertion.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from _harness import HOOKS_DIR, PLUGIN_ROOT, SKIP_EXIT_CODE, Results, base_env  # noqa: E402

ADAPTER = HOOKS_DIR / "recovery_codex.py"
CLI = HOOKS_DIR / "working_tree_recovery.py"
COMMAND = "python3 ${PLUGIN_ROOT}/hooks/recovery_codex.py"
MIN_PYTHON = (3, 11)
SPEAKING = ("PreToolUse", "SessionStart")
ALLOWED_TOP = frozenset({"systemMessage", "hookSpecificOutput"})
ALLOWED_NESTED = frozenset({"hookEventName", "additionalContext"})
FORBIDDEN = frozenset({"decision", "permissionDecision", "continue", "stopReason"})
HANDLER_KEYS = frozenset({"type", "command", "timeout", "async", "statusMessage"})
# The five registrations of recovery_codex.py: (event, matcher, timeout in seconds, async).
# README.md and SECURITY.md state the same table, and manifest_consistency_smoke.py pins the
# event, matcher and script inventory; this table also pins what only this adapter owns.
# Each timeout exceeds the engine's own budget for that event (8 s by default, 60 s for Stop,
# 2.5 s for Codex SessionEnd). Only the Stop entry may be async, because it prints nothing,
# so there is no decision for an async hook to lose.
REGISTRATION = (
    ("PreToolUse", "^Bash$", 15, False),
    ("PostToolUse", "^(Bash|apply_patch)$", 15, False),
    ("Stop", "", 120, True),
    ("SessionEnd", "", 3, False),
    ("SessionStart", "startup|resume|clear", 15, False),
)
# The four handlers that existed before this adapter, byte for byte. Adding recovery must not
# change them. Each is compared as a whole object, so a changed timeout, a dropped async flag
# or a reworded status message on any of them fails here.
EARLIER_HANDLERS = (
    ("SessionStart", "startup|resume|clear|compact", {"type": "command", "command": "node ${PLUGIN_ROOT}/hooks/step_zero_reminder.js", "timeout": 5, "statusMessage": "Loading project-orientation reminder"}),
    ("SessionStart", "startup|resume|clear|compact", {"type": "command", "command": "python3 ${PLUGIN_ROOT}/hooks/session_start_context.py", "timeout": 30, "statusMessage": "Reading register and peer inbox context"}),
    ("Stop", "", {"type": "command", "command": "python3 ${PLUGIN_ROOT}/hooks/context_status_reporter.py", "timeout": 30, "async": True, "statusMessage": "Reporting fresh Codex context status"}),
    ("Stop", "", {"type": "command", "command": "python3 ${PLUGIN_ROOT}/hooks/inbox_consumer.py", "timeout": 8, "statusMessage": "Checking peer inbox before ending turn"}),
)
# Hook schemas copied from the Codex 0.163.0 binary (/opt/homebrew/Caskroom/codex/0.163.0/bin/codex),
# read as a file and never executed. Each is the JSON that follows its own title in the binary,
# for example pre-tool-use.command.input, keyed here by event and direction. tool_input and
# tool_response are typed `true` (any JSON), so these schemas cannot say whether
# tool_input.command is a string or an array, or what an apply_patch payload looks like; those
# two open questions are covered by fixtures of every plausible shape instead. The output
# schemas set additionalProperties to false at every level, which is what makes "only allowed
# keys" a checkable property rather than a convention.
SCHEMA_JSON: dict[str, str] = {
    "PreToolUse.input": '{"$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"definitions":{"NullableString":{"type":["string","null"]}},"properties":{"agent_id":{"type":"string"},"agent_type":{"type":"string"},"cwd":{"type":"string"},"hook_event_name":{"const":"PreToolUse","type":"string"},"model":{"type":"string"},"permission_mode":{"enum":["default","acceptEdits","plan","dontAsk","bypassPermissions"],"type":"string"},"session_id":{"type":"string"},"tool_input":true,"tool_name":{"type":"string"},"tool_use_id":{"type":"string"},"transcript_path":{"$ref":"#/definitions/NullableString"},"turn_id":{"description":"Codex extension: expose the active turn id to internal turn-scoped hooks.","type":"string"}},"required":["cwd","hook_event_name","model","permission_mode","session_id","tool_input","tool_name","tool_use_id","transcript_path","turn_id"],"title":"pre-tool-use.command.input","type":"object"}',
    "PreToolUse.output": '{"$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"definitions":{"PreToolUseDecisionWire":{"enum":["approve","block"],"type":"string"},"PreToolUseHookSpecificOutputWire":{"additionalProperties":false,"properties":{"additionalContext":{"default":null,"type":"string"},"hookEventName":{"const":"PreToolUse","type":"string"},"permissionDecision":{"allOf":[{"$ref":"#/definitions/PreToolUsePermissionDecisionWire"}],"default":null},"permissionDecisionReason":{"default":null,"type":"string"},"updatedInput":{"default":null}},"required":["hookEventName"],"type":"object"},"PreToolUsePermissionDecisionWire":{"enum":["allow","deny","ask"],"type":"string"}},"properties":{"continue":{"default":true,"type":"boolean"},"decision":{"allOf":[{"$ref":"#/definitions/PreToolUseDecisionWire"}],"default":null},"hookSpecificOutput":{"allOf":[{"$ref":"#/definitions/PreToolUseHookSpecificOutputWire"}],"default":null},"reason":{"default":null,"type":"string"},"stopReason":{"default":null,"type":"string"},"suppressOutput":{"default":false,"type":"boolean"},"systemMessage":{"default":null,"type":"string"}},"title":"pre-tool-use.command.output","type":"object"}',
    "PostToolUse.input": '{"$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"definitions":{"NullableString":{"type":["string","null"]}},"properties":{"agent_id":{"type":"string"},"agent_type":{"type":"string"},"cwd":{"type":"string"},"hook_event_name":{"const":"PostToolUse","type":"string"},"model":{"type":"string"},"permission_mode":{"enum":["default","acceptEdits","plan","dontAsk","bypassPermissions"],"type":"string"},"session_id":{"type":"string"},"tool_input":true,"tool_name":{"type":"string"},"tool_response":true,"tool_use_id":{"type":"string"},"transcript_path":{"$ref":"#/definitions/NullableString"},"turn_id":{"description":"Codex extension: expose the active turn id to internal turn-scoped hooks.","type":"string"}},"required":["cwd","hook_event_name","model","permission_mode","session_id","tool_input","tool_name","tool_response","tool_use_id","transcript_path","turn_id"],"title":"post-tool-use.command.input","type":"object"}',
    "SessionStart.input": '{"$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"definitions":{"NullableString":{"type":["string","null"]}},"properties":{"cwd":{"type":"string"},"hook_event_name":{"const":"SessionStart","type":"string"},"model":{"type":"string"},"permission_mode":{"enum":["default","acceptEdits","plan","dontAsk","bypassPermissions"],"type":"string"},"session_id":{"type":"string"},"source":{"enum":["startup","resume","clear","compact","fork"],"type":"string"},"transcript_path":{"$ref":"#/definitions/NullableString"}},"required":["cwd","hook_event_name","model","permission_mode","session_id","source","transcript_path"],"title":"session-start.command.input","type":"object"}',
    "SessionStart.output": '{"$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"definitions":{"SessionStartHookSpecificOutputWire":{"additionalProperties":false,"properties":{"additionalContext":{"default":null,"type":"string"},"hookEventName":{"const":"SessionStart","type":"string"}},"required":["hookEventName"],"type":"object"}},"properties":{"continue":{"default":true,"type":"boolean"},"hookSpecificOutput":{"allOf":[{"$ref":"#/definitions/SessionStartHookSpecificOutputWire"}],"default":null},"stopReason":{"default":null,"type":"string"},"suppressOutput":{"default":false,"type":"boolean"},"systemMessage":{"default":null,"type":"string"}},"title":"session-start.command.output","type":"object"}',
    "SessionEnd.input": '{"$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"definitions":{"NullableString":{"type":["string","null"]}},"properties":{"cwd":{"type":"string"},"hook_event_name":{"const":"SessionEnd","type":"string"},"reason":{"const":"other","type":"string"},"session_id":{"type":"string"},"transcript_path":{"$ref":"#/definitions/NullableString"}},"required":["cwd","hook_event_name","reason","session_id","transcript_path"],"title":"session-end.command.input","type":"object"}',
    "Stop.input": '{"$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"definitions":{"NullableString":{"type":["string","null"]}},"properties":{"cwd":{"type":"string"},"hook_event_name":{"const":"Stop","type":"string"},"last_assistant_message":{"$ref":"#/definitions/NullableString"},"model":{"type":"string"},"permission_mode":{"enum":["default","acceptEdits","plan","dontAsk","bypassPermissions"],"type":"string"},"session_id":{"type":"string"},"stop_hook_active":{"type":"boolean"},"transcript_path":{"$ref":"#/definitions/NullableString"},"turn_id":{"description":"Codex extension: expose the active turn id to internal turn-scoped hooks.","type":"string"}},"required":["cwd","hook_event_name","last_assistant_message","model","permission_mode","session_id","stop_hook_active","transcript_path","turn_id"],"title":"stop.command.input","type":"object"}',
}
SCHEMAS: dict[str, Any] = {name: json.loads(text) for name, text in SCHEMA_JSON.items()}
MODEL = "gpt-5-codex"
SESSION = "sess-synthetic-1"
PATCH = "*** Begin Patch\n*** Update File: f.txt\n@@\n-one\n+two\n*** End Patch\n"
# The fields each event's input schema requires, with synthetic values. SessionEnd carries no
# model or permission_mode and SessionStart no turn_id. Both schemas forbid extra properties,
# so a fixture that adds one fails validation, which check_fixtures asserts on purpose.
DEFAULTS: dict[str, dict[str, Any]] = {
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "git status"}, "tool_use_id": "call-1", "turn_id": "turn-1"},
    "PostToolUse": {"tool_name": "Bash", "tool_input": {"command": "true"}, "tool_response": {"output": ""}, "tool_use_id": "call-2", "turn_id": "turn-1"},
    "Stop": {"last_assistant_message": None, "stop_hook_active": False, "turn_id": "turn-1"},
    "SessionEnd": {"reason": "other"},
    "SessionStart": {"source": "startup"},
}


def build(event: str, cwd: Path, session: str = SESSION, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"cwd": str(cwd), "hook_event_name": event, "session_id": session, "transcript_path": None}
    if event != "SessionEnd":
        payload.update({"model": MODEL, "permission_mode": "default"})
    payload.update(DEFAULTS[event])
    payload.update(fields)
    return payload


def _resolve(schema: Any, root: dict[str, Any]) -> Any:
    """Follow a local $ref such as #/definitions/NullableString to the schema it names."""
    while isinstance(schema, dict) and "$ref" in schema:
        node: Any = root
        for part in str(schema["$ref"]).removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    table: dict[str, Any] = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None), "number": (int, float)}
    return isinstance(value, table[kind])


def _object_errors(schema: dict[str, Any], value: dict[str, Any], root: dict[str, Any], path: str) -> list[str]:
    errors = [f"{path}.{name}: required" for name in schema.get("required", []) if name not in value]
    properties: dict[str, Any] = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        errors += [f"{path}.{name}: not allowed" for name in value if name not in properties]
    for name, sub in properties.items():
        if name in value:
            errors += validate(sub, value[name], root, f"{path}.{name}")
    return errors


def _value_errors(schema: dict[str, Any], value: Any, path: str) -> list[str]:
    """The checks that look only at one value: type, const and enum."""
    errors: list[str] = []
    kinds: Any = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if kinds and not any(_type_ok(kind, value) for kind in kinds):
        errors.append(f"{path}: expected {kinds}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    return errors


def validate(schema: Any, value: Any, root: dict[str, Any], path: str = "$") -> list[str]:
    """The draft-07 subset the embedded hook schemas use: type, const, enum, required, properties, additionalProperties, allOf, $ref.

    Written here, instead of importing jsonschema, because this smoke ships to adopters and must
    not assume that package. A schema of `true` accepts anything and `false` accepts nothing.
    Returns one message per violation, each prefixed with the JSON path it concerns.
    """
    schema = _resolve(schema, root)
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: no value is allowed"]
    errors = _value_errors(schema, value, path)
    for sub in schema.get("allOf", []):
        errors += validate(sub, value, root, path)
    if isinstance(value, dict):
        errors += _object_errors(schema, value, root, path)
    return errors


def schema_errors(name: str, value: Any) -> list[str]:
    root = SCHEMAS[name]
    return validate(root, value, root)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


def _only_allowed_keys(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    nested: Any = value.get("hookSpecificOutput", {})
    return not set(value) - ALLOWED_TOP and (not isinstance(nested, dict) or not set(nested) - ALLOWED_NESTED)


def _output_failures(event: str, stdout: str) -> list[str]:
    """The labels broken by what a speaking event printed."""
    try:
        value: Any = json.loads(stdout)
    except ValueError:
        return [f"{event} prints one JSON object"]
    failures: list[str] = []
    if not _only_allowed_keys(value):
        failures.append(f"{event} output uses only allowed keys")
    if _keys(value) & FORBIDDEN:
        failures.append(f"{event} output never carries a decision")
    if schema_errors(f"{event}.output", value):
        failures.append(f"{event} output validates against the embedded output schema")
    return failures


def contract_failures(event: str, proc: subprocess.CompletedProcess[str]) -> list[str]:
    """Every label whose assertion the adapter's exit code and stdout break for this event.

    Each label doubles as the assertion that a named mutation must trip, so a label is only ever
    reworded together with check_mutations. An event that never speaks must print nothing. An
    event that speaks may print one JSON object holding only the allowed keys and no decision of
    any kind, valid against the embedded output schema.
    """
    failures = [] if proc.returncode == 0 else [f"{event} exits 0"]
    if event not in SPEAKING:
        return failures + ([f"{event} prints nothing"] if proc.stdout else [])
    return failures + (_output_failures(event, proc.stdout) if proc.stdout else [])


class Sandbox:
    """A temporary HOME, store root and scratch repositories; nothing outside it is read or written.

    Hermetic by construction: HOME, TMPDIR and the store root (SOLET_RECOVERY_ROOT) point into one
    temporary directory, git reads no system configuration, and every repository lives inside it,
    so nothing here touches a real store, a real HOME or a real repository.
    """

    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        self.root = base / "store"
        self.scratch = base / "scratch"
        for directory in (self.home, self.scratch):
            directory.mkdir()

    def env(self, **extra: str) -> dict[str, str]:
        env = base_env()
        env.update({"HOME": str(self.home), "TMPDIR": str(self.scratch), "SOLET_RECOVERY_ROOT": str(self.root), "GIT_CONFIG_NOSYSTEM": "1"})
        env.update(extra)
        return env

    def git(self, repo: Path, *args: str) -> str:
        identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        proc = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=repo, env=self.env(**identity), capture_output=True, text=True, check=True)
        return proc.stdout

    def repo(self, name: str) -> Path:
        """A repository with f.txt and g.txt committed, then both edited by someone else."""
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        for file in ("f.txt", "g.txt"):
            (repo / file).write_text("one\n")
        self.git(repo, "add", "f.txt", "g.txt")
        self.git(repo, "commit", "-q", "-m", "base")
        for file in ("f.txt", "g.txt"):
            (repo / file).write_text("two\n")
        return repo

    def run(self, stdin: str, adapter: Path = ADAPTER, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(adapter)], input=stdin, env=self.env(**(env or {})), cwd=cwd, capture_output=True, text=True, timeout=60, check=False)

    def fire(self, payload: dict[str, Any], adapter: Path = ADAPTER, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.run(json.dumps(payload), adapter, env, cwd)

    def saves(self) -> list[dict[str, Any]]:
        proc = subprocess.run([sys.executable, "-B", str(CLI), "--root", str(self.root), "list"], env=self.env(), capture_output=True, text=True, timeout=60, check=False)
        try:
            rows: Any = json.loads(proc.stdout)
        except ValueError:
            return []
        return rows if isinstance(rows, list) else []


def notice_text(proc: subprocess.CompletedProcess[str]) -> str:
    try:
        value: Any = json.loads(proc.stdout)
    except ValueError:
        return ""
    return str(value.get("systemMessage", "")) if isinstance(value, dict) else ""


def foreign_paths(text: str) -> str:
    found = re.search(r"including \d+ changed outside this session \((.*?)\)", text)
    return found.group(1) if found else ""


def check_fixtures(res: Results, sb: Sandbox) -> None:
    cwd = sb.base / "fixture-cwd"
    fixtures: dict[str, dict[str, Any]] = {
        "PreToolUse command string": build("PreToolUse", cwd),
        "PreToolUse command array": build("PreToolUse", cwd, tool_input={"command": ["bash", "-lc", "git stash"]}),
        "PostToolUse Bash": build("PostToolUse", cwd),
        "PostToolUse apply_patch text": build("PostToolUse", cwd, tool_name="apply_patch", tool_input=PATCH),
        "PostToolUse apply_patch object": build("PostToolUse", cwd, tool_name="apply_patch", tool_input={"input": PATCH}),
        "Stop": build("Stop", cwd),
        "SessionEnd": build("SessionEnd", cwd),
        "SessionStart": build("SessionStart", cwd),
    }
    for label, payload in fixtures.items():
        name = f"{payload['hook_event_name']}.input"
        res.check(not schema_errors(name, payload), f"fixture {label} validates against the embedded {name} schema", "; ".join(schema_errors(name, payload)))
    hollow = build("SessionEnd", cwd, model=MODEL)
    res.check(bool(schema_errors("SessionEnd.input", hollow)), "the validator rejects a SessionEnd payload with a field Codex does not send", "the validator accepted an extra field")


def _entry_failures(event: str, handler: dict[str, Any], timeout: int, is_async: bool) -> list[str]:
    failures: list[str] = []
    if handler.get("timeout") != timeout:
        failures.append(f"{event} recovery entry declares timeout {timeout}")
    if bool(handler.get("async", False)) != is_async:
        failures.append(f"{event} recovery entry is async" if is_async else f"{event} recovery entry is synchronous")
    if handler.get("type") != "command" or set(handler) - HANDLER_KEYS or not handler.get("statusMessage"):
        failures.append(f"{event} recovery entry has the stock command-string shape")
    return failures


def _recovery_failures(hooks: dict[str, Any], event: str, matcher: str, timeout: int, is_async: bool) -> list[str]:
    found = [handler for group in hooks.get(event, []) if group.get("matcher", "") == matcher for handler in group.get("hooks", []) if handler.get("command") == COMMAND]
    if len(found) != 1:
        return [f"{event} registers recovery_codex.py once under matcher {matcher!r}"]
    return _entry_failures(event, found[0], timeout, is_async)


def _earlier_failures(hooks: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for event, matcher, expected in EARLIER_HANDLERS:
        groups = [group for group in hooks.get(event, []) if group.get("matcher", "") == matcher]
        if not any(expected in group.get("hooks", []) for group in groups):
            failures.append(f"earlier handler is unchanged: {expected['command']}")
    return failures


def registration_failures(manifest: Any) -> list[str]:
    """Every label whose assertion the manifest breaks: five recovery entries, four earlier handlers untouched."""
    hooks: Any = manifest.get("hooks") if isinstance(manifest, dict) else None
    if not isinstance(hooks, dict):
        return ["the manifest has a hooks object"]
    failures: list[str] = []
    for row in REGISTRATION:
        failures += _recovery_failures(hooks, *row)
    return failures + _earlier_failures(hooks)


def check_registration(res: Results) -> None:
    manifest: Any = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    failures = registration_failures(manifest)
    res.check(not failures, "hooks.json registers the recovery entries and leaves the four earlier handlers unchanged", "; ".join(failures))


def adapter_source() -> str:
    try:
        return ADAPTER.read_text(encoding="utf-8")
    except OSError:
        return ""


def _imported_modules(tree: ast.AST) -> set[str]:
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    return imported | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}


def _string_literals(tree: ast.AST) -> set[str]:
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _called_attributes(tree: ast.AST) -> set[str]:
    return {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


def check_source(res: Results) -> None:
    source = adapter_source()
    try:
        tree = ast.parse(source, feature_version=(3, 9))
    except SyntaxError as exc:
        res.check(False, "the adapter parses with the Python 3.9 grammar so its floor guard can run first", str(exc))
        return
    res.check(bool(source), "the adapter exists", str(ADAPTER))
    imported = _imported_modules(tree)
    res.check(not imported & {"subprocess", "socket", "urllib", "http", "requests"}, "the adapter imports no process or network module", repr(sorted(imported)))
    forbidden = _string_literals(tree) & FORBIDDEN
    res.check(not forbidden, "no decision-shaped key appears as a string in the adapter", repr(sorted(forbidden)))
    writes = _called_attributes(tree) & {"write_text", "write_bytes", "mkdir", "unlink", "rename", "replace"}
    res.check(not writes, "the adapter writes no file itself; the engine owns the store", repr(sorted(writes)))
    first_floor = source.find("_FLOOR")
    first_sibling = source.find("working_tree_recovery")
    res.check(0 <= first_floor < first_sibling, "the Python floor guard precedes every sibling import")


def shipped_modules() -> tuple[str, ...]:
    try:
        tree = ast.parse(CLI.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "MODULES" for target in node.targets):
            names: Any = ast.literal_eval(node.value)
            return tuple(names)
    return ()


def check_modules(res: Results) -> None:
    modules = shipped_modules()
    res.check(len(modules) == 23, "the shipped MODULES inventory lists the 23 engine modules", str(len(modules)))
    missing = [name for name in modules if not (HOOKS_DIR / name).is_file()]
    res.check(not missing, "every module in the shipped MODULES inventory is present in hooks/", repr(missing))
    canonical = PLUGIN_ROOT.parents[1] / "coordination_hooks_common"
    if not canonical.is_dir():
        return  # an installed plugin carries no canonical directory; the repository run proves identity
    drift = [name for name in modules if not (canonical / name).is_file() or (canonical / name).read_bytes() != (HOOKS_DIR / name).read_bytes()]
    res.check(not drift, "every shipped engine module is byte-identical to its canonical source", repr(drift))


def additional_context(proc: subprocess.CompletedProcess[str]) -> str:
    try:
        value: Any = json.loads(proc.stdout)
    except ValueError:
        return ""
    nested: Any = value.get("hookSpecificOutput", {}) if isinstance(value, dict) else {}
    return str(nested.get("additionalContext", "")) if isinstance(nested, dict) else ""


def check_notice_content(res: Results, sb: Sandbox, repo: Path) -> None:
    proc = sb.fire(build("PreToolUse", repo, tool_input={"command": "git stash"}))
    res.check(not contract_failures("PreToolUse", proc), "a discarding command exits 0 and prints only allowed keys", repr(contract_failures("PreToolUse", proc)))
    text = notice_text(proc)
    res.check(text.startswith("Saved") and "f.txt" in foreign_paths(text) and "g.txt" in foreign_paths(text), "the notice names the saved work another session left", text)
    res.check("restore save" in additional_context(proc), "the model is told how to restore", additional_context(proc))
    res.check(sb.git(repo, "status", "--porcelain").split() == ["M", "f.txt", "M", "g.txt"] and not sb.git(repo, "stash", "list"), "the hook itself discards and stashes nothing")
    rows = [row for row in sb.saves() if row.get("trigger") == "pre_discard"]
    res.check(len(rows) == 1 and text.find(str(rows[0].get("save_id", "x"))[:8]) > 0, "the shipped CLI lists the save the notice names", repr(sb.saves()))


def check_command_forms(res: Results, sb: Sandbox) -> None:
    """R1: tool_input.command may arrive as a string or as an argument array.

    A shell tool commonly wraps the whole line as ["bash", "-lc", "<line>"]; every form must be
    classified exactly like the string form.
    """
    for label, command in (("argument array", ["git", "checkout", "--", "f.txt"]), ("shell-wrapped array", ["bash", "-lc", "git stash"])):
        got = sb.fire(build("PreToolUse", sb.repo("array-" + label.split()[0]), tool_input={"command": command}))
        res.check(not contract_failures("PreToolUse", got) and "Saved" in notice_text(got), f"command as an {label} is classified like the string form", got.stdout)


def check_quiet_commands(res: Results, sb: Sandbox, repo: Path) -> None:
    for quiet in ("git status", "ls -la", 42):
        got = sb.fire(build("PreToolUse", repo, tool_input={"command": quiet}))
        res.check(got.returncode == 0 and got.stdout == "", f"non-discarding command {quiet!r} stays silent", got.stdout)


def check_discard_notices(res: Results, sb: Sandbox) -> None:
    repo = sb.repo("discard")
    check_notice_content(res, sb, repo)
    check_command_forms(res, sb)
    check_quiet_commands(res, sb, repo)


def check_attribution(res: Results, sb: Sandbox) -> None:
    """R2: the live shape of an apply_patch PostToolUse event is unmeasured.

    The engine reads the patch text from a string tool_input or from the patch or input field of
    an object, so both are exercised. When the shape is read correctly the patched file is this
    session's own edit, and only the file nobody here touched is reported as changed outside the
    session.
    """
    for shape, tool_input in (("text", PATCH), ("object", {"input": PATCH})):
        repo = sb.repo("patch-" + shape)
        post = sb.fire(build("PostToolUse", repo, session=f"patch-{shape}", tool_name="apply_patch", tool_input=tool_input))
        res.check(post.returncode == 0 and post.stdout == "", f"apply_patch as {shape} is journaled silently", post.stdout)
        pre = sb.fire(build("PreToolUse", repo, session=f"patch-{shape}", tool_input={"command": "git checkout -- f.txt g.txt"}))
        listed = foreign_paths(notice_text(pre))
        res.check("g.txt" in listed and "f.txt" not in listed, f"an apply_patch edit ({shape}) is this session's own; only the untouched file is reported", notice_text(pre))


def check_quiet_events(res: Results, sb: Sandbox) -> None:
    repo = sb.repo("turn-end")
    for event in ("Stop", "SessionEnd", "PostToolUse"):
        proc = sb.fire(build(event, repo, session="turn-end"))
        res.check(not contract_failures(event, proc), f"{event} exits 0 and prints nothing", repr(contract_failures(event, proc)) + proc.stdout)
    res.check(any(row.get("trigger") == "turn_end" for row in sb.saves()), "Stop saved the working tree at the end of the turn", repr(sb.saves()))


def check_session_start(res: Results, sb: Sandbox) -> None:
    repo = sb.repo("start")
    proc = sb.fire(build("SessionStart", repo))
    res.check(proc.returncode == 0 and proc.stdout == "", "SessionStart is silent while recovery is active", proc.stdout)
    if os.geteuid() == 0:
        return  # root ignores directory permissions; the unwritable store cannot be made
    locked = sb.base / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        env = {"SOLET_RECOVERY_ROOT": str(locked / "store")}
        start = sb.fire(build("SessionStart", repo), env=env)
        res.check(not contract_failures("SessionStart", start) and "inactive" in start.stdout, "an unwritable store is reported once at SessionStart, as additionalContext only", start.stdout)
        pre = sb.fire(build("PreToolUse", repo, tool_input={"command": "git stash"}), env=env)
        res.check(not contract_failures("PreToolUse", pre) and "will run" in notice_text(pre), "an unwritable store tells the person the command will run", pre.stdout)
    finally:
        locked.chmod(0o700)


def check_faults(res: Results, sb: Sandbox) -> None:
    """Fault fixtures: whatever arrives, the hook exits 0 and prints nothing or only allowed keys.

    The cases cover malformed and mistyped payloads, unusable folders, git missing from PATH and an
    oversized command line.
    """
    repo = sb.repo("faults")
    plain = sb.base / "not-a-repo"
    plain.mkdir()
    (sb.base / "a-file").write_text("x")
    discard = {"command": "git reset --hard"}
    cases: dict[str, tuple[str, dict[str, str]]] = {
        "empty stdin": ("", {}),
        "not JSON": ("{not json", {}),
        "JSON array": ("[1, 2]", {}),
        "empty object": ("{}", {}),
        "unknown event": (json.dumps(build("PreToolUse", repo, hook_event_name="Nope")), {}),
        "wrong field type": (json.dumps(build("PreToolUse", repo, session_id=5)), {}),
        "missing cwd": (json.dumps({"hook_event_name": "Stop", "session_id": "s"}), {}),
        "cwd is a file": (json.dumps(build("PreToolUse", sb.base / "a-file", tool_input=discard)), {}),
        "cwd missing": (json.dumps(build("PreToolUse", sb.base / "gone", tool_input=discard)), {}),
        "not a repository": (json.dumps(build("PreToolUse", plain, tool_input=discard)), {}),
        "command of the wrong type": (json.dumps(build("PreToolUse", repo, tool_input={"command": {"x": 1}})), {}),
        "tool_input of the wrong type": (json.dumps(build("PreToolUse", repo, tool_input="git stash")), {}),
        "git missing from PATH": (json.dumps(build("PreToolUse", repo, tool_input=discard)), {"PATH": "/nonexistent"}),
        "oversized command": (json.dumps(build("PreToolUse", repo, tool_input={"command": "git stash " + "x" * 200000})), {}),
    }
    for name, (stdin, env) in cases.items():
        proc = sb.run(stdin, env=env)
        event = str(json.loads(stdin).get("hook_event_name", "PreToolUse")) if stdin.startswith("{") and "hook_event_name" in stdin else "PreToolUse"
        event = event if event in DEFAULTS else "PreToolUse"
        res.check(not contract_failures(event, proc), f"fault fixture '{name}' exits 0 with only allowed output", repr(contract_failures(event, proc)) + proc.stderr[-200:])


def check_empty_root(res: Results, sb: Sandbox) -> None:
    """An empty SOLET_RECOVERY_ROOT means unset: the save lands in the default store, never in the hook's current folder."""
    repo = sb.repo("empty-root")
    discard = build("PreToolUse", repo, session="empty-root", tool_input={"command": "git reset --hard"})
    process_cwd = sb.base / "empty-root-cwd"
    process_cwd.mkdir()
    proc = sb.fire(discard, env={"SOLET_RECOVERY_ROOT": ""}, cwd=process_cwd)
    res.check(not contract_failures("PreToolUse", proc), "an empty store root exits 0 with only allowed output", repr(contract_failures("PreToolUse", proc)) + proc.stderr[-200:])
    res.check(list(process_cwd.iterdir()) == [], "an empty store root leaves the hook's current folder empty", repr(list(process_cwd.iterdir())))
    default = sb.home / ("Library/Application Support" if sys.platform == "darwin" else ".local/share") / "Solet/Recovery/v1"
    saves = list((default / "packets").glob("*/*"))
    res.check(len(saves) == 1, "an empty store root saves into the default store under HOME", repr(saves))
    fault_cwd = sb.base / "empty-root-fault-cwd"
    fault_cwd.mkdir()
    proc = sb.fire(discard, env={"SOLET_RECOVERY_ROOT": "", "HOME": str(sb.base / "no-such-home")}, cwd=fault_cwd)
    res.check(not contract_failures("PreToolUse", proc), "an empty store root with no usable HOME exits 0 with only allowed output", repr(contract_failures("PreToolUse", proc)) + proc.stderr[-200:])
    res.check(list(fault_cwd.iterdir()) == [], "an empty store root with no usable HOME leaves the hook's current folder empty", repr(list(fault_cwd.iterdir())))


FLOOR_WRAPPER = "import runpy, sys\nsys.version_info = (3, 9, 18, 'final', 0)\nrunpy.run_path(sys.argv[1], run_name='__main__')\n"


def check_floor(res: Results, sb: Sandbox) -> None:
    """An interpreter below 3.11 must exit 0 and print at most one SessionStart advisory.

    The real adapter file runs unchanged under a wrapper that reports Python 3.9, so this proves
    that the floor guard itself, not a copy of it, runs before any engine module is imported.
    """
    repo = sb.repo("floor")
    for event in ("PreToolUse", "SessionStart"):
        payload = build(event, repo, tool_input={"command": "git stash"}) if event == "PreToolUse" else build(event, repo)
        proc = subprocess.run([sys.executable, "-B", "-c", FLOOR_WRAPPER, str(ADAPTER)], input=json.dumps(payload), env=sb.env(), capture_output=True, text=True, timeout=60, check=False)
        failures = contract_failures(event, proc)
        res.check(not failures, f"an interpreter below 3.11 exits 0 at {event} with only allowed output", repr(failures) + proc.stderr[-200:])
        if event == "SessionStart":
            res.check("3.11" in proc.stdout, "an interpreter below 3.11 is reported once, at SessionStart", proc.stdout)
        else:
            res.check(proc.stdout == "", "an interpreter below 3.11 stays silent at PreToolUse", proc.stdout)


def check_no_bytecode(res: Results, sb: Sandbox) -> None:
    """Run from a copy with bytecode writing left at its default: importing the engine must leave no __pycache__ beside the files."""
    copy_dir = sb.base / "bytecode-copy"
    shutil.copytree(HOOKS_DIR, copy_dir, ignore=shutil.ignore_patterns("__pycache__"))
    env = sb.env()
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    payload = build("PreToolUse", sb.repo("bytecode"), tool_input={"command": "git stash"})
    proc = subprocess.run([sys.executable, str(copy_dir / "recovery_codex.py")], input=json.dumps(payload), env=env, capture_output=True, text=True, timeout=60, check=False)
    res.check(proc.returncode == 0 and "Saved" in notice_text(proc), "the adapter run from a copy still saves and exits 0", proc.stdout + proc.stderr[-200:])
    res.check(not (copy_dir / "__pycache__").exists(), "importing the engine writes no bytecode beside the shipped files")


def mutation_setup(sb: Sandbox, name: str, anchor: str, replacement: str) -> tuple[Path | None, str]:
    """A copy of the shipped hooks directory whose adapter has one anchored line replaced.

    A mutation counts only when its setup is valid (the anchor occurs exactly once), the unmutated
    control passes the named label, and the mutant fails that same label. Returns the reason
    instead of a path when the setup is invalid.
    """
    source = adapter_source()
    if source.count(anchor) != 1:
        return None, f"anchor {anchor!r} occurs {source.count(anchor)} times in the adapter"
    copy_dir = sb.base / f"mutant-{name}"
    shutil.copytree(HOOKS_DIR, copy_dir, ignore=shutil.ignore_patterns("__pycache__"))
    path = copy_dir / "recovery_codex.py"
    path.write_text(source.replace(anchor, replacement), encoding="utf-8")
    return path, ""


def kill(res: Results, name: str, label: str, control: list[str], mutant: list[str]) -> None:
    res.check(label not in control, f"mutation {name}: the unmutated control passes '{label}'", repr(control))
    res.check(label in mutant, f"mutation {name} is killed by '{label}'", repr(mutant))


def adapter_mutation(res: Results, sb: Sandbox, name: str, label: str, payload: dict[str, Any], anchor: str, replacement: str) -> None:
    path, problem = mutation_setup(sb, name, anchor, replacement)
    if path is None:
        res.check(False, f"mutation {name} has a valid setup", problem)
        return
    event = str(payload["hook_event_name"])
    kill(res, name, label, contract_failures(event, sb.fire(payload)), contract_failures(event, sb.fire(payload, path)))


def check_mutations(res: Results, sb: Sandbox) -> None:
    manifest: Any = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    dropped = copy.deepcopy(manifest)
    for group in dropped.get("hooks", {}).get("Stop", []):
        for handler in group.get("hooks", []):
            if handler.get("command") == COMMAND:
                handler.pop("async", None)
    kill(res, "drop async", "Stop recovery entry is async", registration_failures(manifest), registration_failures(dropped))
    repo = sb.repo("mutants")
    rendered = "rendered = _render(event, result.notice, result.context)"
    adapter_mutation(res, sb, "add output", "Stop prints nothing", build("Stop", repo, session="mutants"), rendered, rendered + ' or {"systemMessage": "recovery"}')
    pre = build("PreToolUse", repo, session="mutants", tool_input={"command": "git stash"})
    adapter_mutation(res, sb, "add decision", "PreToolUse output never carries a decision", pre, "output: dict[str, object] = {}", 'output: dict[str, object] = {"decision": "block"}')


def main() -> int:
    if not HOOKS_DIR.is_dir():
        print(f"FAIL: hooks directory not found: {HOOKS_DIR}")
        return 1
    if shutil.which("git") is None:
        print("SKIP: git is not on PATH, so no working tree can be saved or inspected on this machine.")
        return SKIP_EXIT_CODE
    res = Results("Codex recovery adapter")
    with tempfile.TemporaryDirectory(prefix="codex-recovery-smoke-") as tmp:
        sb = Sandbox(Path(tmp).resolve())
        check_fixtures(res, sb)
        check_registration(res)
        check_source(res)
        check_modules(res)
        if sys.version_info >= MIN_PYTHON:
            check_discard_notices(res, sb)
            check_attribution(res, sb)
            check_quiet_events(res, sb)
            check_session_start(res, sb)
            check_faults(res, sb)
            check_empty_root(res, sb)
            check_floor(res, sb)
            check_no_bytecode(res, sb)
            check_mutations(res, sb)
    return res.finish()


if __name__ == "__main__":
    raise SystemExit(main())
